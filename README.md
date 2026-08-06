# Open WebUI Email tools

Search on mail server for information and fetch specific message content using Open WebUI's RAG engine.

## Features

- Extracts keywords from search query and builds search pattern.
- Searches for messages in specified mailboxes.
- Ranks results using similarity scoring and email date.
- Downloads messages and processes content.
- Caches uploaded messages by content hash to avoid processing same messages multiple times.
- Inspects messages and retrieve relevant parts.
- Stores credentials in User Valves settings.
- Supports IMAP protocol

## Available tools

### `search_email_messages`

Searches for messages on the mail server and returns metadata.

Parameters:

| Parameter | Description |
|---|---|
| `query` | Search query. Default to `None`. |
| `unread` | Filter on new messages only. Defaults to `False`. |
| `mailboxes` | Mailboxes to search. Defaults to all except Trash, Bin, Junk and Spam. |

Each result contains:

- Subject
- Path
- Timestamp
- Sender
- Recipients
- Attachments
- Search score

### `inspect_email_messages`

Inspects specific messages content and uses Open WebUI's retrieval engine to fetch relevant parts.

Parameters:

| Parameter | Description |
|---|---|
| `query` | Search query. |
| `messages` | List of messages. |

Each result contains:

- EML filename
- Open WebUI file ID
- Text snippets

## Installation

1. Go to Workspace in Open WebUI.
2. Create a new tool from the Tools tab.
3. Paste the content of `openwebui_email_tools.py` and save the tool.
4. Configure the mail address and password for each user.
5. Configure the tool valves to change default settings.
6. Enable the tool in your custom model.

## Configuration

### User Valves

| Setting | Description |
|---|---|
| `mailaddr` | Email account address. |
| `password` | Email account password. |

### Tool Valves

| Setting | Default | Description |
|---|---:|---|
| `protocol` | `imap` | Connection method: `imap`. |
| `verify_ssl` | `true` | SSL certificates verification. |
| `host` | `host.docker.internal` | Server hostname or IP address reachable from the Open WebUI container. |
| `port` | Protocol default | Optional custom server port. |
| `search_count` | `20` | Maximum number of search results to return. |
| `search_timeout` | `60` | Maximum search task duration in seconds. |

When `port` is not set, the protocol default port (`993`) is used.

## Security

- Enable encryption to store credentials (set a strong `WEBUI_SECRET_KEY` and set `ENABLE_VALVE_ENCRYPTION` to `true`).
- Restrict network access between Open WebUI and the mail server.

## Compatibility

Tested with **Open WebUI 0.10.2**.

The tool imports internal Open WebUI modules, so compatibility with earlier or later releases is not guaranteed.

## License

[GNU AGPLv3](LICENSE)
