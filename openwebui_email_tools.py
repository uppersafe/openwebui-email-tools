"""
title: Email tools
author: Nicolas THIBAUT
git_url: https://github.com/uppersafe/
description: Search on mail server for information and fetch specific message content.
license: AGPL-3.0-only
version: 1.1.0
required_open_webui_version: 0.10.2
requirements: imapclient
"""

import os
import io
import re
import time
import json
import unicodedata
import mimetypes
import asyncio
import logging
import ssl
from hashlib import blake2b
from difflib import SequenceMatcher
from fastapi import Request, UploadFile
from pydantic import BaseModel, Field
from email import policy, message_from_bytes
from email.utils import getaddresses, parsedate_to_datetime
from email.message import EmailMessage
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.users import UserModel
from open_webui.internal.db import get_async_db_context
from open_webui.routers.files import upload_file_handler
from open_webui.routers.retrieval import (
    ProcessFileForm,
    process_file,
    QueryCollectionsForm,
    query_collection_handler,
)

log = logging.getLogger(__name__)


class MailException(IMAPClientError):
    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error


class MailClient:
    def __init__(
        self,
        host: str,
        port: int,
        verify: bool = True,
    ):
        ssl_context = ssl.create_default_context()
        if not verify:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        self.imap = IMAPClient(
            host=host,
            port=port,
            use_uid=True,
            ssl=True,
            ssl_context=ssl_context,
            timeout=10,
        )
        self.mailbox = None

    def login(self, mailaddr: str, password: str) -> None:
        try:
            self.imap.login(mailaddr, password)
        except LoginError as e:
            raise MailException("Invalid username or password", e)
        except IMAPClientError as e:
            raise MailException("Unable to login", e)

    def logout(self) -> None:
        try:
            self.imap.logout()
        except IMAPClientError as e:
            raise MailException("Unable to logout", e)

    def list(self, ignore: list = []) -> list:
        try:
            data = self.imap.list_folders()
            if not isinstance(data, list):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException("Unable to list mailboxes", e)

        mailboxes = []

        for flags, delimiter, mailbox in data:
            if mailbox.upper() not in ignore:
                mailboxes.append(mailbox)

        return mailboxes

    def select(self, mailbox: str) -> None:
        try:
            self.imap.select_folder(mailbox, readonly=True)
        except IMAPClientError as e:
            raise MailException(f"Unable to select mailbox {mailbox}", e)

        self.mailbox = mailbox

    def search(self, mailbox: str, criteria: list) -> list:
        if mailbox != self.mailbox:
            self.select(mailbox)

        try:
            data = self.imap.search(criteria, charset="UTF-8")
            if not isinstance(data, list):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException(f"Unable to search mailbox {mailbox}", e)

        return data

    def fetch(
        self,
        mailbox: str,
        uid: int,
        raw: bool = False,
    ) -> EmailMessage | bytes:
        if mailbox != self.mailbox:
            self.select(mailbox)

        # TODO: retrieve only (BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM TO CC)])
        try:
            data = self.imap.fetch([uid], ["BODYSTRUCTURE", "BODY.PEEK[]"])
            if not isinstance(data, dict):
                raise MailException("Unable to parse response data", data)
            data = data.get(uid, None)
            if not isinstance(data, dict):
                raise MailException("Unable to parse response data", data)
            data = data.get(b"BODY[]", None)
            if not isinstance(data, bytes):
                raise MailException("Unable to parse response data", data)
        except IMAPClientError as e:
            raise MailException(f"Unable to fetch msg {uid} from mailbox {mailbox}", e)

        if not raw:
            return message_from_bytes(data, policy=policy.default)

        return data


class Tools:
    class UserValves(BaseModel):
        mailaddr: str = Field(
            title="Email address",
            default=None,
        )
        password: str = Field(
            title="Email password",
            default=None,
            json_schema_extra={"input": {"type": "password"}},
        )

    class Valves(BaseModel):
        protocol: str = Field(
            title="Protocol",
            default="imap",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "imap", "label": "IMAP"},
                    ],
                }
            },
        )
        verify_ssl: bool = Field(
            title="SSL verification",
            default=True,
        )
        host: str = Field(
            title="Server hostname or IP address",
            default="host.docker.internal",
        )
        port: int | None = Field(
            title="Server port",
            default=None,
            ge=1,
            le=65535,
        )
        search_count: int = Field(
            title="Search result count",
            default=20,
        )
        search_timeout: int = Field(
            title="Search timeout",
            default=60,
        )

    def __init__(self):
        self.valves = self.Valves()
        self.namespace = "tools.email.files"

    async def _connect_imap(self, mailaddr: str, password: str) -> MailClient:
        session = MailClient(
            host=self.valves.host,
            port=self.valves.port or 993,
            verify=self.valves.verify_ssl,
        )
        session.login(mailaddr, password)
        return session

    def _disconnect(self, session) -> None:
        if hasattr(session, "logout"):
            session.logout()

    def _browse_imap(
        self,
        session,
        query: str,
        unread: bool,
        mailboxes: list,
        timeout: int = None,
    ) -> list:
        results = []
        timeout = timeout or int(time.monotonic() + self.valves.search_timeout)

        # Extract search keywords
        keywords = self._extract_keywords(query)

        # Build search criteria
        criteria = self._build_criteria(keywords)

        # Look for unread messages only
        criteria = ["UNSEEN", criteria] if unread else criteria

        # Retrieve mailboxes
        if len(mailboxes) == 0:
            mailboxes = session.list(ignore=["TRASH", "BIN", "JUNK", "SPAM"])

        for mailbox in mailboxes:
            try:
                if int(time.monotonic()) >= timeout:
                    raise TimeoutError(
                        f"Timeout of search task after {self.valves.search_timeout} secs"
                    )

                # Search for messages
                uids = session.search(mailbox, criteria)

                for uid in uids:
                    # Fetch message by UID
                    msg = session.fetch(mailbox, uid)

                    attachments = []
                    for part in msg.walk():
                        if not part.is_multipart():
                            content_type = part.get_content_type()
                            content_disposition = part.get_content_disposition()
                            if content_disposition in ["attachment", "inline"]:
                                filename = part.get_filename()
                                if filename is not None:
                                    # content = part.get_content()
                                    attachments.append(filename)

                    body = msg.get_body(preferencelist=("plain", "html"))
                    content_type = body.get_content_type()
                    content = body.get_content()

                    date = msg.get("Date")
                    subject = msg.get("Subject")
                    sender = msg.get("From")
                    tos = msg.get_all("To", [])
                    ccs = msg.get_all("Cc", [])

                    recipients = []
                    for name, addr in set(getaddresses(tos + ccs)):
                        recipients.append(f"{name} <{addr}>")

                    if subject is not None and date is not None:
                        results.append(
                            self._score_message(
                                mailbox,
                                uid,
                                content,
                                date,
                                subject,
                                sender,
                                recipients,
                                attachments,
                                keywords,
                            )
                        )

            except Exception as e:
                log.warning(e)

        # Sort results and return best matches
        return self._sort_results(results)

    def _download_imap(self, session, mailbox: str, uid: int) -> bytes:
        return session.fetch(mailbox, uid, raw=True)

    def _get_credentials(self, config: dict) -> dict:
        if config.mailaddr is None:
            raise ValueError("Please configure email address")

        if config.password is None:
            raise ValueError("Please configure email password")

        return config.mailaddr.strip(), config.password.strip()

    def _seq_match(self, text: str, keywords: list) -> list:
        # Normalize text in ascii characters
        nfkd_text = (
            unicodedata.normalize("NFKD", text.lower())
            .encode("ascii", "ignore")
            .decode()
        )
        # Normalize keywords in ascii characters
        nfkd_keywords = [
            unicodedata.normalize("NFKD", keyword.lower())
            .encode("ascii", "ignore")
            .decode()
            for keyword in keywords
        ]

        # Get the match length for each keyword
        return [
            SequenceMatcher(None, nfkd_keyword, nfkd_text).find_longest_match().size
            for nfkd_keyword in nfkd_keywords
        ]

    def _score_message(
        self,
        mailbox: str,
        uid: int,
        content: str,
        date: str,
        subject: str,
        sender: str,
        recipients: list,
        attachments: list,
        keywords: list,
    ) -> dict:
        # Initialize score to zero
        score = 0

        # Calculate the keywords total length
        total_length = sum(len(keyword) for keyword in keywords)

        # Calculate the weight of one character
        match_weight = 1.0 / max(1.0, total_length)

        # Calculate match with content
        score = score + sum(
            match_size * match_weight
            for match_size in self._seq_match(content, keywords)
        )

        # Calculate match with subject
        score = score + sum(
            match_size * match_weight
            for match_size in self._seq_match(subject, keywords)
        )

        # Calculate match with sender
        score = score + sum(
            match_size * match_weight
            for match_size in self._seq_match(sender, keywords)
        )

        for recipient in recipients:
            # Calculate match with recipient
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(recipient, keywords)
            )

        for attachment in attachments:
            mimetype, encoding = mimetypes.guess_type(attachment)

            # Lower weight for image, audio and video files
            match_weight = 0.5 if self._is_media(mimetype) else 1.0

            # Calculate the weight of one character
            match_weight = match_weight / max(1.0, total_length)

            # Calculate match with attachment
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(attachment, keywords)
            )

        return {
            "subject": subject.strip(),
            "path": f'/{mailbox}/{uid}/{subject.strip().replace("/", "-")}.eml',
            "timestamp": parsedate_to_datetime(date).timestamp(),
            "sender": sender.strip(),
            "recipients": recipients,
            "attachments": attachments,
            "search_score": score,
        }

    def _sort_results(self, results: list) -> list:
        return sorted(
            results,
            key=lambda result: (result["search_score"], result["timestamp"]),
            reverse=True,
        )[: self.valves.search_count]

    def _is_media(self, mimetype: str) -> bool:
        if mimetype is not None:
            return mimetype.startswith(("image/", "audio/", "video/"))
        return False

    def _build_criteria(self, keywords: list) -> list:
        if len(keywords) >= 1:
            keyword = ["TEXT", keywords[0]]
            if len(keywords) >= 2:
                return ["OR", keyword, self._build_criteria(keywords[1:])]
            return keyword
        return ["ALL"]

    def _extract_keywords(self, query: str) -> list:
        if query is None or len(query) == 0:
            return []

        # Split query on special characters (space, tab, comma, etc) and remove linking words
        keywords = set(
            keyword.strip()
            for keyword in re.split(r"[';,\s\t\r\n]+", query)
            if len(keyword.strip()) > 1
        )

        # Check that keywords are not empty
        if len(keywords) == 0:
            raise ValueError("Cannot build keywords from query string")

        return list(keywords)

    async def _get_cache_file(
        self,
        file_hash: str,
        user: UserModel,
    ) -> tuple:
        cache_key = f"{self.namespace}.{user.id}.{file_hash}"
        cache_value = await Config.get(cache_key, {})

        if "id" in cache_value:
            file = await Files.get_file_by_id(cache_value.get("id"))
            if file is None:
                log.warning(f"Deleting cache for {cache_key}")
                await Config.delete(cache_key)
                cache_value.clear()

        return (
            cache_value.get("id", None),
            cache_value.get("collection", None),
        )

    async def _set_cache_file(
        self,
        file_hash: str,
        file_id: str,
        file_collection: str,
        user: UserModel,
    ) -> None:
        cache_key = f"{self.namespace}.{user.id}.{file_hash}"
        cache_value = {
            "id": file_id,
            "collection": file_collection,
        }
        await Config.upsert({cache_key: cache_value})

    async def _emit_status(
        self,
        event_emitter,
        desc: str,
        done: bool = False,
        hidden: bool = False,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": desc,
                        "done": done,
                        "hidden": hidden,
                    },
                }
            )

    async def search_email_messages(
        self,
        query: str = None,
        unread: bool = False,
        mailboxes: list = [],
        __request__: Request = None,
        __user__: dict = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Search for messages on mail server.
        Best to quickly identify relevant messages from INBOX or other mailboxes.

        :param query: The search query to look up without special operators or wildcards
        :param unread: Flag to only look for new messages
        :param mailboxes: A list of mailboxes to look into (defaults to all except Trash, Bin, Junk and Spam)
        :return: JSON with search results containing subject, path, timestamp, sender, recipients, attachments and score of each message
        """
        session = None
        try:
            if __request__ is None:
                raise ValueError("Request context not available")
            if __user__ is None:
                raise ValueError("User context not available")

            user = UserModel(**__user__)
            mailaddr, password = self._get_credentials(__user__.get("valves"))

            await self._emit_status(
                __event_emitter__,
                "Connecting to mail server...",
                done=False,
            )

            # Connect to mail server
            session = await self._connect_imap(mailaddr, password)

            await self._emit_status(
                __event_emitter__,
                "Searching on mail server...",
                done=False,
            )

            # Browse messages on mail server
            results = await asyncio.to_thread(
                self._browse_imap, session, query, unread, mailboxes
            )

            await self._emit_status(
                __event_emitter__,
                f"{len(results)} messages found.",
                done=True,
            )

            return json.dumps(list(results), ensure_ascii=False)

        except MailException as e:
            log.error(f"{e} = {e.error}")
            return json.dumps({"error": str(e)})

        except Exception as e:
            log.exception(e)
            return json.dumps({"error": str(e)})

        finally:
            # Disconnect from mail server
            self._disconnect(session)

    async def inspect_email_messages(
        self,
        query: str,
        messages: list,
        __request__: Request = None,
        __user__: dict = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Search for information in specific messages on mail server.
        Best for efficient content retrieval.

        :param query: The search query to use for RAG
        :param messages: A list of path for messages to look into
        :return: JSON with search results containing EML filename, file ID and snippets for each message
        """
        session = None
        try:
            if __request__ is None:
                raise ValueError("Request context not available")
            if __user__ is None:
                raise ValueError("User context not available")

            user = UserModel(**__user__)
            mailaddr, password = self._get_credentials(__user__.get("valves"))

            await self._emit_status(
                __event_emitter__,
                "Connecting to mail server...",
                done=False,
            )

            # Connect to mail server
            session = await self._connect_imap(mailaddr, password)

            await self._emit_status(
                __event_emitter__,
                f"Processing {len(messages)} messages...",
                done=False,
            )

            collections = []

            for path in messages:
                try:
                    # Extract mailbox, UID and filename
                    match = re.search(r"/([^/]+)/([^/]+)/(.*)", path)
                    mailbox, uid, filename = match.group(1, 2, 3)
                    mimetype, encoding = mimetypes.guess_type(filename)

                    if not mimetype.startswith("message/"):
                        raise TypeError(f"Invalid message type '{mimetype}'")
                    else:
                        log.info(f"Downloading '{path}'")
                        content = await asyncio.to_thread(
                            self._download_imap, session, mailbox, uid
                        )

                    # Search for file in cache
                    file_hash = blake2b(content).hexdigest()
                    file_id, file_collection = await self._get_cache_file(
                        file_hash,
                        user=user,
                    )

                    # Process file if not in cache
                    if file_id is None or file_collection is None:
                        async with get_async_db_context() as db:
                            log.info(f"Uploading '{filename}'")
                            file = await upload_file_handler(
                                __request__,
                                UploadFile(
                                    file=io.BytesIO(content),
                                    filename=filename,
                                    headers={"content-type": mimetype},
                                ),
                                metadata={},
                                process=False,
                                user=user,
                                db=db,
                            )

                            log.info(f"Processing '{file.path}'")
                            result = await process_file(
                                __request__,
                                ProcessFileForm(file_id=file.id),
                                user=user,
                                db=db,
                            )

                            file_collection = result.get("collection_name")

                            await self._set_cache_file(
                                file_hash,
                                file.id,
                                file_collection,
                                user=user,
                            )

                    collections.append(file_collection)

                except Exception as e:
                    log.warning(f"Cannot process '{path}' ({e})")

            # Query the collection using the retrieval engine
            collection_results = await query_collection_handler(
                __request__,
                QueryCollectionsForm(
                    collection_names=collections,
                    query=query,
                ),
                user=user,
            )

            results = {}

            # Generate query-focused results (instead of relying on raw results)
            for distances, metadatas, documents in zip(
                collection_results.get("distances", []),
                collection_results.get("metadatas", []),
                collection_results.get("documents", []),
            ):
                for distance, metadata, document in zip(
                    distances, metadatas, documents
                ):
                    name = metadata.get("name")
                    source = metadata.get("file_id")
                    source_hash = blake2b(source.encode()).hexdigest()
                    # Get existing snippets if source already in results
                    snippets = results.get(source_hash, {}).get("snippets", [])
                    # Add new source to results or update existing source with new snippets
                    results.update(
                        {
                            source_hash: {
                                "filename": name,
                                "id": source,
                                "snippets": snippets + [document],
                            }
                        }
                    )

            await self._emit_status(
                __event_emitter__,
                f"{len(results)} results found.",
                done=True,
            )

            return json.dumps(list(results.values()), ensure_ascii=False)

        except MailException as e:
            log.error(f"{e} = {e.error}")
            return json.dumps({"error": str(e)})

        except Exception as e:
            log.exception(e)
            return json.dumps({"error": str(e)})

        finally:
            # Disconnect from mail server
            self._disconnect(session)
