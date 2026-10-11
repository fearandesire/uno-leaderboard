# UNO Scoreboard

This bot records UNO points and trophies in MongoDB and posts a paginated scoreboard.

## Commands

- `/help` lists the available commands and explains where they can be used.
- `/point` adds one point for the caller in the configured score channel.
- `/scores` posts the scoreboard in the configured scoreboard channel.
- `/addpoints`, `/removepoints`, `/addtrophy`, and `/removetrophy` are for the configured admin user IDs or server administrators. Select a member from the current guild; amounts must be positive.

Commands are available in servers. The bot does not request privileged gateway intents.

## Configuration

Set `BOT_TOKEN` and `MONGO_URI` in the runtime environment. The score and scoreboard channel IDs are configured in `bot.py`. Invite the application with the `bot` and `applications.commands` scopes, and allow it to view the configured channel and send messages with embeds.

Start with `python3 bot.py`. Application commands are synced once during login; Discord retains them across gateway reconnects.
