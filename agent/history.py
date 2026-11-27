"""The conversation as Claude sees it.

Current Claude models tie each thinking block to everything before it, and reject a
request that replays one after its prefix changed. So history is append-only, with one
exception: an interrupted phone turn is cut to what the caller heard. That cut is made once,
before the next request, and drops the turn's thinking blocks, which keeps every
remaining block valid.
"""

THINKING = ("thinking", "redacted_thinking")


class Conversation:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def add_customer(self, text: str) -> None:
        """What they said. Joins the previous user message if Aida hasn't replied since."""
        self._add_user([{"type": "text", "text": text}])

    def add_note(self, text: str) -> None:
        """From the system. Customers can't write one: their brackets are replaced."""
        self.add_customer(f"[{text}]")

    def add_tool_results(self, results: list[dict]) -> None:
        self._add_user(results)

    def add_assistant(self, content: list[dict]) -> bool:
        """Add a reply. Returns False if it held nothing to keep."""
        content = _after_fallback(content)
        kept = [b for b in content if b["type"] != "text" or b["text"].strip()]
        if not any(b["type"] in ("text", "tool_use") for b in kept):
            return False
        self.messages.append({"role": "assistant", "content": kept})
        return True

    def cut_turn(self, start: int, heard: dict[int, str]) -> None:
        """Rewrite the turn beginning at `start` so it holds only what was heard.

        `heard` maps the turn's assistant messages (0, 1, ...) to the text heard from
        each. Tool calls stay: they ran, whether or not the caller heard about them.
        """
        rebuilt: list[dict] = []
        index = 0
        for message in self.messages[start:]:
            if message["role"] == "assistant":
                text = heard.get(index, "").strip()
                index += 1
                content = [{"type": "text", "text": text}] if text else []
                content += [b for b in message["content"] if b["type"] == "tool_use"]
                if not content:
                    continue
                message = {"role": "assistant", "content": content}
            rebuilt.append(message)
        self.messages[start:] = rebuilt

    def strip_thinking(self) -> None:
        """Last resort if the API still rejects a thinking block."""
        for message in self.messages:
            message["content"] = [b for b in message["content"] if b["type"] not in THINKING]
        self.messages[:] = [m for m in self.messages if m["content"]]

    def _add_user(self, content: list[dict]) -> None:
        if self.messages and self.messages[-1]["role"] == "user":
            # A new dict: an earlier request may still hold a reference to the old one.
            last = self.messages[-1]
            self.messages[-1] = {"role": "user", "content": [*last["content"], *content]}
        else:
            self.messages.append({"role": "user", "content": content})


def _after_fallback(content: list[dict]) -> list[dict]:
    """If another model took over mid-reply, drop what the first one left unfinished."""
    boundary = max((i for i, b in enumerate(content) if b["type"] == "fallback"), default=-1)
    return [
        block
        for i, block in enumerate(content)
        if i >= boundary or block["type"] not in (*THINKING, "tool_use")
    ]


def tool_uses(content: list[dict]) -> list[dict]:
    return [block for block in _after_fallback(content) if block["type"] == "tool_use"]
