# Security and Privacy

[Back to README](../README.md)

`config.json` contains sensitive credentials, including Telegram Bot Tokens and Slack Webhook URLs.

Restrict access to the configuration file:

```bash
chmod 700 ~/.codex/hooks
chmod 600 ~/.codex/hooks/config.json
```

If the files are stored in a Git repository, add the following to `.gitignore`:

```gitignore
config.json
__pycache__/
*.pyc
```

Avoid committing actual credentials to version control.

**Privacy consideration:** User prompts and Codex responses may contain source code, credentials, private project details, or other sensitive information.

To prevent prompt and response contents from being sent to external services, set:

```json
{
  "notification": {
    "include_input": false,
    "include_output": false
  }
}
```

These values can be merged into your existing `notification` configuration.

