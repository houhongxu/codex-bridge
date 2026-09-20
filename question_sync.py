"""Adapt live async questions to the official CLI's dismissible request UI.

This adapter owns only its synthetic question requests. Real tool requests and
approvals never enter this response path. Desktop answers remain ordinary user
messages; a matched, committed answer dismisses only its corresponding question.
"""

import asyncio
import copy
import contextlib
import json
import uuid
from collections import OrderedDict


OPEN = "<send_user_message_question_reply>"
CLOSE = "</send_user_message_question_reply>"


def object_value(value):
    return value if isinstance(value, dict) else {}


def question_key(message_id, index):
    return json.dumps(["request_user_input_async", message_id, index], separators=(",", ":"))


def reply_values(item):
    """Parse only a complete reply envelope in a single user text item."""
    if item.get("type") != "userMessage":
        return []
    content = item.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return []
    part = content[0]
    if not isinstance(part, dict) or part.get("type") != "text":
        return []
    text = part.get("text", "")
    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text.startswith(OPEN) or not text.endswith(CLOSE):
        return []
    try:
        values = json.loads(text[len(OPEN):-len(CLOSE)])
    except (ValueError, TypeError):
        return []
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        return []
    return values


def answered_ids(item):
    """Accept only the desktop's complete reply envelope in a user message."""
    result = []
    for value in reply_values(item):
        if not isinstance(value, dict) or not isinstance(value.get("answer"), str):
            continue
        try:
            identity = json.loads(value.get("questionItemId", ""))
        except (ValueError, TypeError):
            continue
        if (isinstance(identity, list) and len(identity) == 3
                and identity[0] == "request_user_input_async"
                and isinstance(identity[1], str) and type(identity[2]) is int
                and identity[2] >= 0):
            result.append(question_key(identity[1], identity[2]))
    return result


def display_reply(item):
    """Format a CLI-facing copy; never change the submitted/backend message."""
    values = reply_values(item)
    if not values or len(answered_ids(item)) != len(values):
        return item
    # Unknown fields may carry information we cannot represent. Preserve those
    # envelopes, malformed entries and quoted examples without partial rewriting.
    if any(set(value) != {"questionItemId", "question", "answer"}
           or not isinstance(value.get("question"), str) or not value["question"].strip()
           for value in values):
        return item
    updated = copy.deepcopy(item)
    part = updated["content"][0]
    part["text"] = "\n\n".join(
        "问题：" + value["question"] + "\n你的回答：" + value["answer"]
        for value in values)
    # Original offsets refer to the JSON envelope, not the formatted text.
    if "text_elements" in part:
        part["text_elements"] = []
    return updated


def async_questions(item):
    if item.get("type") != "agentMessage" or item.get("delivery") != "async":
        return None
    questions = item.get("questions")
    if (not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(questions, list) or not 1 <= len(questions) <= 32):
        return None
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("title"), str):
            return None
        options = question.get("options")
        if not question["title"].strip() or len(question["title"]) > 20000:
            return None
        if options is not None and (not isinstance(options, list) or not 1 <= len(options) <= 32
                                    or any(not isinstance(v, str) or not v.strip()
                                           or len(v) > 20000 for v in options)):
            return None
    return questions


def without_history_questions(message, method=None):
    """Format answer envelopes without restoring the CLI's stale local panel.

    History pages are not a complete pending-question ledger. Only live questions
    are made interactive; historic questions can still be answered in Desktop.
    """
    updated = copy.deepcopy(message)
    result = object_value(updated.get("result"))
    items = []

    def turns(values):
        for turn in values if isinstance(values, list) else []:
            if isinstance(turn, dict) and isinstance(turn.get("items"), list):
                items.extend(turn["items"])

    turns(object_value(result.get("thread")).get("turns"))
    turns(object_value(result.get("initialTurnsPage")).get("data"))
    if method == "thread/turns/list":
        turns(result.get("data", []))
    elif method == "thread/items/list" and isinstance(result.get("data"), list):
        items.extend(entry.get("item") for entry in result.get("data", [])
                     if isinstance(entry, dict))
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if async_questions(item) is not None:
            item["questions"] = None
            changed = True
        displayed = display_reply(item)
        if displayed is not item:
            item.update(displayed)
            changed = True
    return updated if changed else message


class QuestionSync:
    MAX_PENDING = 256
    MAX_SEEN = 4096
    RPC_TIMEOUT = 15

    def __init__(self, send_cli, send_backend):
        self.send_cli = send_cli
        self.send_backend = send_backend
        self.threads = set()
        self.active_turns = {}
        self.questions = {}
        self.requests = {}
        self.seen = OrderedDict()
        self.history_requests = {}
        self.calls = {}
        self.tasks = set()
        self.prefix = "cb-question-" + uuid.uuid4().hex + "-"

    def track_thread(self, thread):
        tid = thread["id"]
        self.threads.add(tid)
        for turn in thread.get("turns") or []:
            if turn.get("status") == "inProgress":
                self.active_turns[tid] = turn.get("id")

    def remember(self, key):
        self.seen[key] = None
        self.seen.move_to_end(key)
        while len(self.seen) > self.MAX_SEEN:
            self.seen.popitem(last=False)

    def request(self, question):
        request_id = self.prefix + uuid.uuid4().hex
        question["request_id"] = request_id
        self.requests[request_id] = question
        source = question["source"]
        options = source.get("options")
        return {"id": request_id, "method": "item/tool/requestUserInput", "params": {
            "threadId": question["thread_id"], "turnId": question["turn_id"],
            "itemId": question["item_id"] + ":cb:" + str(question["index"]),
            "isBlocking": True, "autoResolutionMs": None,
            "questions": [{"id": "answer", "header": "cb sync", "question": source["title"],
                           "isOther": True, "isSecret": False,
                           "options": None if options is None else [
                               {"label": option, "description": ""} for option in options]}]}}

    def resolved(self, question):
        return {"method": "serverRequest/resolved", "params": {
            "threadId": question["thread_id"], "requestId": question["request_id"]}}

    def complete(self, key):
        self.remember(key)
        question = self.questions.pop(key, None)
        if question is not None:
            self.requests.pop(question["request_id"], None)
        return question

    async def from_cli(self, message):
        method = message.get("method")
        if method in {"thread/read", "thread/turns/list", "thread/items/list"}:
            if object_value(message.get("params")).get("threadId") in self.threads:
                self.history_requests[message.get("id")] = method
        # Only this adapter's own request IDs may become structured answers.
        rid = message.get("id")
        if method is not None or not isinstance(rid, str) or not rid.startswith(self.prefix):
            return False
        question = self.requests.pop(rid, None)
        if question is None or question.get("submitting"):
            return True  # A late local answer to a question already handled elsewhere.
        result = object_value(message.get("result"))
        answers = object_value(object_value(result.get("answers")).get("answer")).get("answers")
        if not isinstance(answers, list) or not answers or any(not isinstance(v, str) for v in answers):
            # Dismiss/cancel is not a user answer and must not reach the model.
            self.questions.pop(question["key"], None)
            self.remember(question["key"])
            await self.send_cli(self.resolved(question))
            return True
        answer = "\n".join(answers).strip()
        if not answer:
            self.questions.pop(question["key"], None)
            self.remember(question["key"])
            await self.send_cli(self.resolved(question))
            return True
        question["submitting"] = True
        task = asyncio.create_task(self.submit(question, answer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    def from_backend(self, message):
        """Return (forwarded message or None, extra CLI notifications)."""
        rid = message.get("id")
        if ("method" not in message and isinstance(rid, str)
                and rid.startswith(self.prefix + "rpc-")):
            future = self.calls.pop(rid, None)
            if future is not None and not future.done():
                future.set_result(message)
            return None, []
        method = message.get("method")
        params = object_value(message.get("params"))
        tid = params.get("threadId")
        extras = []
        if rid in self.history_requests:
            return without_history_questions(message, self.history_requests.pop(rid)), []
        thread = object_value(object_value(message.get("result")).get("thread"))
        if thread.get("id") in self.threads:
            return without_history_questions(message), []
        if tid not in self.threads:
            return message, []
        if method == "turn/started":
            self.active_turns[tid] = object_value(params.get("turn")).get("id")
        elif method == "turn/completed":
            if self.active_turns.get(tid) == object_value(params.get("turn")).get("id"):
                self.active_turns.pop(tid, None)
        if method not in {"item/started", "item/completed"}:
            return message, []
        item = object_value(params.get("item"))
        if method == "item/completed":
            for identity in answered_ids(item):
                question = self.complete((tid, identity))
                if question is not None:
                    extras.append(self.resolved(question))
        displayed = display_reply(item)
        if displayed is not item:
            message = copy.deepcopy(message)
            message["params"]["item"] = displayed
        questions = async_questions(item)
        if questions is None or not isinstance(params.get("turnId"), str):
            return message, extras
        if len(self.questions) + len(questions) > self.MAX_PENDING:
            return message, extras  # Preserve native behavior if the adapter is at capacity.
        message = copy.deepcopy(message)
        message["params"]["item"]["questions"] = None
        self.active_turns.setdefault(tid, params["turnId"])
        if method == "item/completed":
            for index, source in enumerate(questions):
                key = (tid, question_key(item["id"], index))
                if key in self.seen or key in self.questions:
                    continue
                question = {"key": key, "thread_id": tid, "turn_id": params["turnId"],
                            "item_id": item["id"], "index": index, "source": source}
                self.questions[key] = question
                extras.append(self.request(question))
        return message, extras

    async def call(self, method, params):
        rid = self.prefix + "rpc-" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.calls[rid] = future
        try:
            await self.send_backend({"id": rid, "method": method, "params": params})
            response = await asyncio.wait_for(future, self.RPC_TIMEOUT)
            if "error" in response:
                raise RuntimeError("Backend rejected the answer")
            return response.get("result", {})
        finally:
            self.calls.pop(rid, None)

    async def submit(self, question, answer):
        if question["key"] not in self.questions:
            return  # Desktop resolved it before this submission task began.
        tid, identity = question["key"]
        text = OPEN + "\n" + json.dumps([{
            "questionItemId": identity, "question": question["source"]["title"],
            "answer": answer}], ensure_ascii=False) + "\n" + CLOSE
        params = {"threadId": tid, "input": [{"type": "text", "text": text}],
                  "clientUserMessageId": str(uuid.uuid4())}
        turn_id = self.active_turns.get(tid)
        method = "turn/steer" if turn_id else "turn/start"
        if turn_id:
            params["expectedTurnId"] = turn_id
        try:
            await self.call(method, params)
            # An accepted RPC is also evidence when the user-message echo is suppressed.
            if self.complete(question["key"]) is not None:
                await self.send_cli(self.resolved(question))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never retry automatically: after a transport timeout the answer may
            # already have reached the backend. Preserve the exact decision once.
            self.complete(question["key"])
            with contextlib.suppress(Exception):
                await self.send_cli(self.resolved(question))
                await self.send_cli({"method": "warning", "params": {"threadId": tid,
                    "message": "cb could not confirm this answer. Check the conversation in Desktop before replying again; it was not retried automatically."}})

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for future in self.calls.values():
            future.cancel()
        self.calls.clear()
