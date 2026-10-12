import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Do not inherit a production database URI or token when importing application
# configuration into this test process.
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["BOT_TOKEN"] = "test-token"

import discord

import bot


class FakeResponse:
    def __init__(self):
        self.done = False
        self.calls = []

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))
        self.done = True

    async def send_message(self, content, **kwargs):
        self.calls.append(("send_message", content, kwargs))
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, content, **kwargs):
        self.calls.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user, channel_id=bot.SCORES_CHANNEL_ID):
        self.user = user
        self.channel_id = channel_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class GatewayAndCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_only_guilds_gateway_intent_is_enabled(self):
        expected = discord.Intents.none()
        expected.guilds = True
        self.assertEqual(bot.intents.value, expected.value)
        self.assertFalse(bot.intents.message_content)
        self.assertFalse(bot.intents.members)
        self.assertFalse(bot.intents.presences)

    def test_all_user_commands_are_guild_only_slash_commands(self):
        expected = {
            "help",
            "point",
            "scores",
            "addpoints",
            "removepoints",
            "addtrophy",
            "removetrophy",
        }
        registered = {command.name: command for command in bot.bot.tree.get_commands()}
        self.assertEqual(set(registered), expected)
        self.assertTrue(all(command.guild_only for command in registered.values()))
        self.assertFalse(hasattr(bot.bot, "on_message"))

    async def test_help_lists_each_command_and_usage_rule(self):
        interaction = FakeInteraction(SimpleNamespace(id=123, display_name="UNO player"))

        await bot.bot.tree.get_command("help").callback(interaction)

        content = interaction.response.calls[0][1]
        self.assertIn("/help", content)
        self.assertIn("/point", content)
        self.assertIn("/scores", content)
        self.assertIn("/addpoints", content)
        self.assertIn("/removepoints", content)
        self.assertIn("/addtrophy", content)
        self.assertIn("/removetrophy", content)
        self.assertIn("configured score channel", content)
        self.assertIn("positive amount", content)
        self.assertTrue(interaction.response.calls[0][2]["ephemeral"])

    def test_member_admin_override_and_missing_permissions(self):
        admin_id = next(iter(bot.ADMIN_IDS))
        admin = SimpleNamespace(id=admin_id)
        regular_member = SimpleNamespace(
            id=admin_id + 1,
            guild_permissions=SimpleNamespace(administrator=False),
        )
        server_admin = SimpleNamespace(
            id=admin_id + 2,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        no_guild_permissions = SimpleNamespace(id=admin_id + 3)

        self.assertTrue(bot.is_admin(SimpleNamespace(user=admin)))
        self.assertFalse(bot.is_admin(SimpleNamespace(user=regular_member)))
        self.assertTrue(bot.is_admin(SimpleNamespace(user=server_admin)))
        self.assertFalse(bot.is_admin(SimpleNamespace(user=no_guild_permissions)))

    def test_admin_target_is_a_member_option_not_a_cache_lookup(self):
        command = bot.bot.tree.get_command("addpoints")
        self.assertIs(
            inspect.signature(command.callback).parameters["member"].annotation,
            discord.Member,
        )

    async def test_setup_hook_syncs_commands_once(self):
        with patch.object(bot.bot.tree, "sync", new_callable=AsyncMock) as sync:
            await bot.bot.setup_hook()

        sync.assert_awaited_once_with()


class PointCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_point_is_rejected_outside_configured_channel(self):
        user = SimpleNamespace(id=123, display_name="UNO player")
        interaction = FakeInteraction(user, channel_id=bot.SCORES_CHANNEL_ID + 1)

        with patch.object(bot, "add_point") as add_point:
            await bot.bot.tree.get_command("point").callback(interaction)

        add_point.assert_not_called()
        self.assertEqual(interaction.response.calls[0][0], "send_message")
        self.assertTrue(interaction.response.calls[0][2]["ephemeral"])

    async def test_point_updates_callers_stored_id_and_display_name(self):
        user = SimpleNamespace(id=123, display_name="UNO player")
        interaction = FakeInteraction(user)

        with patch.object(bot, "add_point") as add_point:
            await bot.bot.tree.get_command("point").callback(interaction)

        add_point.assert_called_once_with("123", "UNO player")
        self.assertEqual(interaction.response.calls[0], ("defer", {}))
        self.assertEqual(interaction.followup.calls[0][0], "✅ Added one point.")
        self.assertFalse(interaction.followup.calls[0][1].get("ephemeral", False))


class AdminCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def admin_interaction():
        user = SimpleNamespace(
            id=next(iter(bot.ADMIN_IDS)),
            guild_permissions=SimpleNamespace(administrator=False),
        )
        return FakeInteraction(user)

    @staticmethod
    def target_member():
        return SimpleNamespace(id=987, display_name="Target")

    async def test_unauthorized_user_cannot_add_points(self):
        user = SimpleNamespace(
            id=max(bot.ADMIN_IDS) + 1,
            guild_permissions=SimpleNamespace(administrator=False),
        )
        target = SimpleNamespace(id=987, display_name="Target")
        interaction = FakeInteraction(user)

        with patch.object(bot, "add_point") as add_point:
            await bot.bot.tree.get_command("addpoints").callback(interaction, target, 5)

        add_point.assert_not_called()
        self.assertIn("don't have permission", interaction.response.calls[0][1])

    async def test_authorized_user_adds_to_interaction_member_without_cache_lookup(self):
        user = SimpleNamespace(
            id=next(iter(bot.ADMIN_IDS)),
            guild_permissions=SimpleNamespace(administrator=False),
        )
        target = SimpleNamespace(id=987, display_name="Target")
        interaction = FakeInteraction(user)

        with patch.object(bot, "add_point") as add_point:
            await bot.bot.tree.get_command("addpoints").callback(interaction, target, 5)

        add_point.assert_called_once_with("987", "Target", 5)
        self.assertEqual(interaction.response.calls[0], ("defer", {"ephemeral": True}))

    async def test_nonpositive_amount_is_rejected_before_database_write(self):
        user = SimpleNamespace(
            id=next(iter(bot.ADMIN_IDS)),
            guild_permissions=SimpleNamespace(administrator=False),
        )
        target = SimpleNamespace(id=987, display_name="Target")
        interaction = FakeInteraction(user)

        with patch.object(bot, "add_point") as add_point:
            await bot.bot.tree.get_command("addpoints").callback(interaction, target, 0)

        add_point.assert_not_called()
        self.assertIn("positive", interaction.response.calls[0][1])

    async def test_unauthorized_user_cannot_use_other_admin_commands(self):
        commands = ("removepoints", "addtrophy", "removetrophy")
        user = SimpleNamespace(
            id=max(bot.ADMIN_IDS) + 1,
            guild_permissions=SimpleNamespace(administrator=False),
        )
        target = self.target_member()

        for name in commands:
            with self.subTest(command=name):
                interaction = FakeInteraction(user)
                with (
                    patch.object(bot, "remove_points") as remove_points,
                    patch.object(bot, "add_trophy") as add_trophy,
                    patch.object(bot, "remove_trophy") as remove_trophy,
                ):
                    await bot.bot.tree.get_command(name).callback(interaction, target, 2)

                remove_points.assert_not_called()
                add_trophy.assert_not_called()
                remove_trophy.assert_not_called()
                self.assertIn("don't have permission", interaction.response.calls[0][1])

    async def test_other_admin_commands_write_positive_amounts(self):
        cases = (
            ("removepoints", "remove_points", True, ("987", 2), "Points removed."),
            ("addtrophy", "add_trophy", None, ("987", "Target", 2), "Trophies added."),
            ("removetrophy", "remove_trophy", True, ("987", 2), "Trophies removed."),
        )
        target = self.target_member()

        for name, helper_name, helper_result, expected_args, message in cases:
            with self.subTest(command=name):
                interaction = self.admin_interaction()
                with patch.object(bot, helper_name, return_value=helper_result) as helper:
                    await bot.bot.tree.get_command(name).callback(interaction, target, 2)

                helper.assert_called_once_with(*expected_args)
                self.assertEqual(interaction.response.calls[0], ("defer", {"ephemeral": True}))
                self.assertEqual(interaction.followup.calls[0][0], f"✅ {message}")
                self.assertTrue(interaction.followup.calls[0][1]["ephemeral"])

    async def test_other_admin_commands_reject_nonpositive_amounts(self):
        cases = (
            ("removepoints", "remove_points"),
            ("addtrophy", "add_trophy"),
            ("removetrophy", "remove_trophy"),
        )
        target = self.target_member()

        for name, helper_name in cases:
            with self.subTest(command=name):
                interaction = self.admin_interaction()
                with patch.object(bot, helper_name) as helper:
                    await bot.bot.tree.get_command(name).callback(interaction, target, 0)

                helper.assert_not_called()
                self.assertIn("positive", interaction.response.calls[0][1])

    async def test_remove_commands_report_missing_balances(self):
        cases = (
            ("removepoints", "remove_points", "**Target** has no points to remove."),
            ("removetrophy", "remove_trophy", "**Target** has no trophies to remove."),
        )
        target = self.target_member()

        for name, helper_name, expected_message in cases:
            with self.subTest(command=name):
                interaction = self.admin_interaction()
                with patch.object(bot, helper_name, return_value=False) as helper:
                    await bot.bot.tree.get_command(name).callback(interaction, target, 2)

                helper.assert_called_once()
                self.assertEqual(interaction.followup.calls[0][0], expected_message)
                self.assertTrue(interaction.followup.calls[0][1]["ephemeral"])


def tearDownModule():
    bot.mongo.close()


if __name__ == "__main__":
    unittest.main()
