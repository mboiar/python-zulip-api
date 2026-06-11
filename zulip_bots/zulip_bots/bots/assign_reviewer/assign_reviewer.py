from typing import Any, Dict, Optional, List, Set
import random
import logging
import json
import os
from datetime import datetime
import re

import zulip
from zulip_bots.lib import AbstractBotHandler
import gitlab

__version__ = "1.0.0"

logger = logging.getLogger(__name__)
gl = gitlab.Gitlab.from_config("satlab", ["python-gitlab.cfg"])
cfg_path = os.environ.get(
            "REVIEWERS_FILE",
            os.path.join(os.path.dirname(__file__), "reviewers.json"),
        )

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

MR_NOT_FOUND_WITTY_REPLIES = [
    "Merge request does not exist. Review your manners instead.",
    "404: Merge request not found.",
    "This MR is a myth, a legend, a pull request that never was.",
    "No MR here. Maybe it went on a long vacation with the missing semicolons.",
    "MR not found. Are you sure you didn't dream it?",
    "Invalid MR link. The only thing to review is your copy-paste skills.",
    "That merge request has left the repository. It's in a better place now.",
    "Zero MRs found. Time to review your life choices.",
    "This MR doesn't exist. But you know what does? Regret.",
    "No merge request. Maybe it was just a merge suggestion whispered into the void.",
    "MR not found. If it was a feature, it's a very hidden one.",
    "I can't assign reviewers to nothing. Even I have standards.",
]

class ReviewAssignerHandler:
    # Cached bot identity to avoid API calls on every message
    _bot_full_name: Optional[str] = None
    _bot_user_id: Optional[int] = None
    
    group = None
    reviewer_names = None

    def _load_config(self, stream_name):
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

        reviewer_data = data.get(stream_name)

        if reviewer_data is not None:
            self.reviewer_names = reviewer_data.get("members")
            self.group = gl.groups.get(reviewer_data.get("gitlab_group_id"), lazy=True)


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

    def _gitlab_get_mr_list(self, return_open: bool, result_count: int=None):
        if result_count is None:
            return self.group.mergerequests.list(state=('opened' if return_open else "closed"), scope='all', draft=False, get_all=True)
        else:
            return self.group.mergerequests.list(state=('opened' if return_open else "closed"), scope='all', draft=False, get_all=False, per_page=result_count)
    
    def usage(self) -> str:
         return """
            ReviewBot v2.0.
            I help with merge request reviews. Automatically synced with Gitlab.

            • **@ReviewBot assign <link|id>** - picks two random reviewers for **link**
            • **@ReviewBot assign <link|id> @**<username1>** ...** - assigns reviewers for **link**
            • **@ReviewBot reviewed <link|id>** - lets me know that you reviewed **link**
            • **@ReviewBot leaderboard** - shows top reviewers (resets every month)
            • **@ReviewBot list** - lists active merge requests
            • **@ReviewBot list n** - lists **n** most recent active merge requests
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
        self, client: zulip.Client, stream_name: str, reviewer_names: List[str] = None
    ) -> Optional[List[Dict]]:
        """Load list of user IDs for `stream_name` from a dict or an external JSON file.

        The JSON file should be a mapping of stream names to lists of user IDs, e.g.
        {
            "": ["John Doe", "Jan Paweł"],
        }

        The default file is `reviewers.json` next to this module. You
        can override it by setting the environment variable `REVIEWERS_FILE`.
        """

        if reviewer_names is None or len(reviewer_names) == 0:
            self._load_config(stream_name)
            reviewer_names = self.reviewer_names

        if reviewer_names is None:
            logger.error("No member list for stream '%s' in %s", stream_name, cfg_path)
            return None
        
        all_members = []

        try:
            result = client.get_members()
            if result["result"] != "success":
                logger.error("Failed to get subscribers: %s", result.get("msg"))
                return None
            all_members = result["members"]
        except Exception:
            logger.exception("Exception fetching stream subscribers")
            return None

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

        stream_name = message.get("display_recipient")
        self._load_config(stream_name)

        mr_list_all = self._gitlab_get_mr_list(True)

        # # ---------- REVIEW CONFIRMATION ----------
        if cmd == "reviewed":
        #     parent_id = message.get("reply_to")
            if len(content_data) != 2:
                bot_handler.send_reply(message, "Please provide a merge request link. Example:\n"
                                            "`@**ReviewAssigner** reviewed <link>`")
                return

            mr_title = content_data[1]
            mr_id = mr_title.split("/")[-1]
            mr_ids_all =  [str(mr.iid) for mr in mr_list_all]

            if mr_id in mr_ids_all:
                sender_id = message.get("sender_id")
                if sender_id:
                    counts = self._get_review_counts(bot_handler)
                    counts[str(sender_id)] = counts.get(str(sender_id), 0) + 1
                    self._save_review_counts(bot_handler, counts)

                    try:
                        user = client.get_user_by_id(sender_id)
                        name = user["user"]["full_name"] if user["result"] == "success" else f"User {sender_id}"
                    except Exception:
                        name = f"User {sender_id}"

                    bot_handler.send_reply(
                        message,
                        f"✅ Thanks @**{name}** for reviewing [{mr_title.split("/")[-1]}]({mr_title})! "
                        f"(You now have {counts[str(sender_id)]} review{'s' if counts[str(sender_id)] != 1 else ''})"
                    )
                else:
                    bot_handler.send_reply(message, "Could not identify you, sorry")
            else:
                bot_handler.send_reply(message, random.choice(MR_NOT_FOUND_WITTY_REPLIES))
            return

        # ---------- LEADERBOARD ----------
        if cmd == "leaderboard":
            self._show_leaderboard(message, bot_handler, stream_name)
            return
       
        if cmd == "assign":

            if len(content_data) < 2:
                bot_handler.send_reply(message, "Please provide a merge request link. Example:\n"
                                            "`@ReviewAssigner assign <link>`")
                return

            payload = content_data[1].split(" ", maxsplit=1)
            mr_title = payload[0]
            mr_id = int(mr_title.split("/")[-1])
            mr_ids_all =  [mr.iid for mr in mr_list_all]
            if mr_id not in mr_ids_all:
                bot_handler.send_reply(message, random.choice(MR_NOT_FOUND_WITTY_REPLIES))
                return

            requested_reviewers = re.findall(r"@\*\*(.+?)\*\*", payload[1])

            # Get stream and topic where we were called
            stream_name = message.get("display_recipient")
            if not stream_name:
                bot_handler.send_reply(message, "I can only work in streams.")
                return

            # Fetch subscribers of this stream
            members = self._get_reviewers(client, stream_name, requested_reviewers)
            if not members:
                bot_handler.send_reply(
                    message,
                    "Sorry, I couldn't fetch the subscriber list of this stream."
                )
                return

            # Decide who to exclude (always exclude the bot itself, maybe the sender)
            exclude = {self._bot_user_id} if self._bot_user_id is not None else set()
            # if message.get("sender_id") and message["sender_id"] != self._bot_user_id:
            #     exclude.add(message["sender_id"])
                
            chosen = self._pick_two(members, exclude)
            if not chosen:
                bot_handler.send_reply(message, "Nobody eligible to pick - everyone is excluded.")
                return

            mentions = " ".join(f"@**{m["full_name"]}**" for m in chosen)
            names = [m["full_name"] for m in chosen]
            ps = random.choice(PHRASES)
            reply = f"Reviewers for [#{mr_title.split("/")[-1]}]({mr_title}): {mentions}\n*{ps}*"

            mr = next(x for x in mr_list_all if x.iid == mr_id)
            self.update_mr_reviewers(mr, names)

            resp = bot_handler.send_reply(message, reply)
            return
        
        if cmd == "list":
            result_count = None
            if len(content_data) > 1 and content_data[1].isdigit():
                result_count = content_data[1]
                print(result_count)
            print(result_count)
            self.send_active_merge_requests(message, bot_handler, result_count)
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

    def send_active_merge_requests(self, message, bot_handler, result_count: int = 5) -> None:
        """Send a human-readable list of active merge requests as a reply to `message`."""

        some_open_mrs = self._gitlab_get_mr_list(True, result_count)
        
        if not some_open_mrs:
            bot_handler.send_reply(message, "No active merge requests.")
            return

        client = bot_handler._client
        lines = ["**Active Merge Requests**", ""]
        for e in some_open_mrs:
            assigned = e.reviewers
            names = []
            for user in assigned:
                    names.append(user["name"])
            name_str = ", ".join(names) if names else "(no reviewers)"
            lines.append(f"- [{e.title}]({e.web_url}): {name_str}")

        bot_handler.send_reply(message, "\n".join(lines))

    def update_mr_reviewers(self, mr, reviewers: List[str]):
        pr = gl.projects.get(mr.project_id, lazy=True)
        editable_mr = pr.mergerequests.get(mr.iid, lazy=True)
        reviewer_ids = []
        # TODO: cache ids
        for name in reviewers:
            reviewer_ids.append(gl.users.list(search=name, get_all=True)[0].id)
        editable_mr.reviewer_ids = reviewer_ids
        result = editable_mr.save()


handler_class = ReviewAssignerHandler