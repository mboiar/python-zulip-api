from typing import Any, Dict, Optional, List, Set
import random
import logging
import json
import os
from datetime import datetime

import zulip
from zulip_bots.lib import AbstractBotHandler

__version__ = "1.0.0"

logger = logging.getLogger(__name__)

PHRASES = [
    "Review responsibly. No dark side stuff.",
    "You two were chosen by the almighty RNG. Blame the universe.",
    "The prophecy foretold of this pairing.",
    "You're the lucky winners of today's review lottery!",
    "Don't worry, the bugs are afraid of you. Probably.",
    "The random number gods have spoken.",
    "You're the chosen ones. There's no escape.",
    "Good luck. You'll need it (just kidding… maybe).",
    "I promise this isn't a punishment.",
    "This is the way.",
    "Remember, with great reviewing power comes great responsibility.",
    "Brace yourselves, merge requests are coming.",
    "I've got a good feeling about this pairing.",
    "May your comments be constructive and your coffee strong.",
    "You two, together, can break any build!",
    "This match was made in developer heaven.",
    "If you don't review, I'll assign you again tomorrow.",
    "The bot has spoken. Resistance is futile.",
    "This pairing was blessed by a passing cosmic ray.",
    "Don't make me assign the cat. She only types 'meow'.",
    "You've been volunteered! Congratulations.",
    "May your comments be witty and your merge conflicts few.",
    "Telemetry shows both of you are online. Resistance is futile.",
    "The tumbling space potato of fate has landed on you.",
     "A merge request without a reviewer is like a satellite without a ground station.",
]


class ReviewAssignerHandler:
    # Cached bot identity to avoid API calls on every message
    _bot_full_name: Optional[str] = None
    _bot_user_id: Optional[int] = None
    active_assignments: Dict[int, Dict[str, Any]] = {}

    def _get_review_counts(self, bot_handler) -> Dict[int, int]:
            """Retrieve review counts from persistent storage."""
            try:
                data = bot_handler.storage.get("review_counts")
            except KeyError:
                return {}
            
            return json.loads(data)

    def _save_review_counts(self, bot_handler, counts: Dict[int, int]) -> None:
        data = json.dumps(counts)
        bot_handler.storage.put("review_counts", data)
    
    def _get_last_reset_date(self, bot_handler) -> str:
            """Retrieve last reset date from persistent storage."""
            try:
                data = bot_handler.storage.get("last_reset_date")
            except KeyError:
                return "01-1970"
            return data

    def _save_last_reset_date(self, bot_handler, date: str) -> None:
        bot_handler.storage.put("last_reset_date", date)
    
    def usage(self) -> str:
         return """
            I randomly assign two stream members to review a merge request.

            • **@ReviewAssigner Bot assign title** - picks two random reviewers for *title*
            • **@ReviewAssigner Bot reviewed title** - lets me know that you reviewed *title*
            • **@ReviewAssigner Bot leaderboard - shows top reviewers (resets every month)
            • **@ReviewAssigner Bot list - lists merge requests to be reviewed
                                """

    def _init_identity(self, bot_handler: AbstractBotHandler) -> None:
        """Fetch and cache the bot's own full name and user ID."""
        if self._bot_full_name is not None:
            return
        try:
            client = bot_handler._client
            profile = client.get_profile()
            if profile["result"] == "success":
                self._bot_full_name = profile["full_name"]
                self._bot_user_id = profile["user_id"]
                logger.info("Bot identity: %s (ID %d)", self._bot_full_name, self._bot_user_id)
            else:
                logger.error("Failed to fetch bot profile: %s", profile.get("msg"))
        except Exception:
            logger.exception("Cannot initialize bot identity")

    def _get_reviewers(
        self, client: zulip.Client, stream_name: str
    ) -> Optional[List[Dict]]:
        """Load list of user IDs for `stream_name` from an external JSON file.

        The JSON file should be a mapping of stream names to lists of user IDs, e.g.
        {
            "": ["John Doe", "Jan Paweł"],
        }

        The default file is `reviewers.json` next to this module. You
        can override it by setting the environment variable `REVIEWERS_FILE`.
        """
        # Determine config path
        cfg_path = os.environ.get(
            "REVIEWERS_FILE",
            os.path.join(os.path.dirname(__file__), "reviewers.json"),
        )
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            logger.error("Members config file not found: %s", cfg_path)
            return None
        except Exception:
            logger.exception("Failed to load members config: %s", cfg_path)
            return None

        # If config is a single list, use it for all streams
        if isinstance(data, list):
            try:
                return [int(x) for x in data]
            except Exception:
                logger.exception("Invalid member IDs in members config list")
                return None

        # Expect mapping of stream -> list
        if not isinstance(data, dict):
            logger.error("Members config must be a dict or list: %s", type(data))
            return None

        reviewer_names = data.get(stream_name)
        if reviewer_names is None:
            logger.error("No member list for stream '%s' in %s", stream_name, cfg_path)
            return None
        
        all_members = []

        try:
            result = client.get_members()
            if result["result"] != "success":
                logger.error("Failed to get subscribers: %s", result.get("msg"))
                return None
            print(result)
            all_members = result["members"]
        except Exception:
            logger.exception("Exception fetching stream subscribers")
            return None
        print(all_members, reviewer_names)

        return [usr for usr in all_members if usr["full_name"] in reviewer_names]

    def _pick_two(self, members: List[Dict], exclude: Set[int]) -> List[int]:
        candidates = [p for p in members if (p["user_id"] not in exclude)]
        if len(candidates) < 2:
            return candidates
        return random.sample(candidates, 2)

    def handle_message(
        self, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        # Only react to stream messages
        if message.get("type") != "stream":
            return

        content = message.get("content", "").strip()
        logger.debug(f"{message}")

        if content == "":
            bot_handler.send_reply(message, self.usage())
            return

        content_data = content.split(maxsplit=1)
        cmd = content_data[0].lower()
        client = bot_handler._client
        last_reset_date = self._get_last_reset_date(bot_handler)
        cur_date = datetime.now().strftime("%m-%Y")

        if cur_date != last_reset_date:
            self._save_review_counts(bot_handler, {})
            self._save_last_reset_date(bot_handler, cur_date)

        # Make sure we know who we are
        self._init_identity(bot_handler)
        if not self._bot_full_name:
            bot_handler.send_reply(message, "I cannot identify myself - please check the logs.")
            return
        
        if os.path.exists("active_assignments.json"):
            with open("active_assignments.json") as fh:
                self.active_assignments = json.load(fh)

        # # ---------- REVIEW CONFIRMATION ----------
        if cmd == "reviewed":
        #     parent_id = message.get("reply_to")
            if len(content_data) < 2:
                bot_handler.send_reply(message, "Please provide a merge request title. Example:\n"
                                            "`@**ReviewAssigner** reviewed 73`")
                return

            mr_title = content_data[1]
            print(self.active_assignments)
            if mr_title in self.active_assignments:
                sender_id = message.get("sender_id")
                if sender_id:
                    # if sender_id in self.active_assignments[mr_title]["reviewed_by"]:
                    #     bot_handler.send_reply(message, "You have already reviewed this MR. Let's hear a second opinion.")
                    self.active_assignments[mr_title]["reviewed_by"].append(sender_id)

                    ready_to_merge = False

                    if len(self.active_assignments[mr_title]["reviewed_by"]) == 2:
                        del self.active_assignments[mr_title]
                        ready_to_merge = True

                    with open("active_assignments.json", "w") as fh:
                        json.dump(self.active_assignments, fh)

                    counts = self._get_review_counts(bot_handler)
                    print(counts)
                    counts[str(sender_id)] = counts.get(str(sender_id), 0) + 1
                    self._save_review_counts(bot_handler, counts)

                    try:
                        user = client.get_user_by_id(sender_id)
                        name = user["user"]["full_name"] if user["result"] == "success" else f"User {sender_id}"
                    except Exception:
                        name = f"User {sender_id}"

                    bot_handler.send_reply(
                        message,
                        f"✅ Thanks @_**{name}** for reviewing **{mr_title}**! "
                        f"(You now have {counts[str(sender_id)]} review{'s' if counts[str(sender_id)] != 1 else ''})"
                    )
                    if ready_to_merge:
                        bot_handler.send_reply(message, f"MR {mr_title}: ready to merge")
                else:
                    bot_handler.send_reply(message, "Could not identify you, sorry")
            else:
                bot_handler.send_reply(message, "Merge request does not exist. Review your manners instead.")
            return

        # ---------- LEADERBOARD ----------
        if cmd == "leaderboard":
            stream_name = message.get("display_recipient")
            self._show_leaderboard(message, bot_handler, stream_name)
            return
       
        if cmd == "assign":

            if len(content_data) < 2:
                bot_handler.send_reply(message, "Please provide a merge request id. Example:\n"
                                            "`@**ReviewAssigner** assign 73`")
                return

            mr_title = content_data[1]

            # Get stream and topic where we were called
            stream_name = message.get("display_recipient")
            if not stream_name:
                bot_handler.send_reply(message, "I can only work in streams.")
                return

            # Fetch subscribers of this stream
            members = self._get_reviewers(client, stream_name)
            if not members:
                bot_handler.send_reply(
                    message,
                    "Sorry, I couldn't fetch the subscriber list of this stream."
                )
                return

            # Decide who to exclude (always exclude the bot itself, maybe the sender)
            exclude = {self._bot_user_id} if self._bot_user_id is not None else set()
            if message.get("sender_id") and message["sender_id"] != self._bot_user_id:
                exclude.add(message["sender_id"])

            chosen = self._pick_two(members, exclude)
            if not chosen:
                bot_handler.send_reply(message, "Nobody eligible to pick - everyone is excluded.")
                return
            # logger.debug(f"{[client.get_user_by_id(m).get("user").get("full_name") for m in members]}")
            # Silent mentions using user IDs
            mentions = " ".join(f"@_**{m["full_name"]}**" for m in chosen)
            ps = random.choice(PHRASES)
            reply = f"Reviewers for **{mr_title}**: {mentions}\n*{ps}*"

            resp = bot_handler.send_reply(message, reply)
            self.active_assignments[mr_title] = {
                    "assigned": [m["user_id"] for m in chosen],
                    "stream": stream_name,
                    "mr_title": mr_title,
                    "topic": message.get("subject", ""),
                    "reviewed_by": []
            }
            print(self.active_assignments)
            with open("active_assignments.json", "w") as fh:
                json.dump(self.active_assignments, fh)
            return
        
        if cmd == "list":
            self.send_active_merge_requests(bot_handler)
            return

        bot_handler.send_reply(message, self.usage())
        return

    def _show_leaderboard(self, message, bot_handler, stream_name):
        counts = self._get_review_counts(bot_handler)
        if not counts:
            bot_handler.send_reply(message, "No reviews completed yet! 😢")
            return

        sorted_reviewers = sorted(counts.items(), key=lambda x: x[1], reverse=True)

        client = bot_handler._client
        leaderboard_lines = ["**🏆 Review Leaderboard**", ""]
        medals = ["🥇", "🥈", "🥉"]
        for rank, (user_id, count) in enumerate(sorted_reviewers[:10], start=1):
            try:
                user = client.get_user_by_id(user_id)
                name = user["user"]["full_name"] if user["result"] == "success" else f"User {user_id}"
            except Exception:
                name = f"User {user_id}"
            prefix = medals[rank-1] if rank <= 3 else f"{rank}."
            leaderboard_lines.append(f"{prefix} **{name}** - {count} review{'s' if count != 1 else ''}")
        leaderboard_lines.append("")
        leaderboard_lines.append("Reply `reviewed` to an assignment to count your review.")
        bot_handler.send_reply(message, "\n".join(leaderboard_lines))

    def get_active_merge_requests(self) -> List[Dict[str, Any]]:
        """Return a list of active merge request entries from in-memory cache or file.

        Each entry is a dict with keys: `mr_title`, `assigned`, `stream`, `topic`, `reviewed_by`.
        """
        if os.path.exists("active_assignments.json"):
            try:
                with open("active_assignments.json", "r", encoding="utf-8") as fh:
                    self.active_assignments = json.load(fh)
            except Exception:
                logger.exception("Failed to load active assignments from file")

        entries: List[Dict[str, Any]] = []
        for mr_title, data in self.active_assignments.items():
            entry = {"mr_title": mr_title}
            if isinstance(data, dict):
                entry.update(data)
            entries.append(entry)
        return entries

    def send_active_merge_requests(self, message, bot_handler) -> None:
        """Send a human-readable list of active merge requests as a reply to `message`."""
        entries = self.get_active_merge_requests()
        if not entries:
            bot_handler.send_reply(message, "No active merge requests.")
            return

        client = bot_handler._client
        lines = ["**Active Merge Requests**", ""]
        for e in entries:
            assigned = e.get("assigned", [])
            names = []
            for uid in assigned:
                try:
                    user = client.get_user_by_id(int(uid))
                    if user.get("result") == "success":
                        names.append(user["user"]["full_name"])
                    else:
                        names.append(f"User {uid}")
                except Exception:
                    names.append(f"User {uid}")
            name_str = ", ".join(names) if names else "(no one assigned)"
            stream = e.get("stream", "")
            lines.append(f"- **{e.get('mr_title')}**: {name_str}")

        bot_handler.send_reply(message, "\n".join(lines))


handler_class = ReviewAssignerHandler