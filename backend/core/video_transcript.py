"""Bounded ASR context windows, independent of the video summary chapters."""
from dataclasses import dataclass
import re

ASR_CONTEXT_SECONDS = 2


@dataclass(frozen=True)
class AsrWindow:
    start: float
    end: float
    keep_range: tuple[float, float]

    @property
    def duration(self):
        return self.end - self.start


def asr_window(start, end, duration):
    """Each chapter owns word midpoints in [start, end), with two seconds of context.

    Offsets always refer to the actual extracted audio, including the clipped
    first/last windows. Context is billed by the provider as ordinary audio.
    """
    left, right = max(0, start - ASR_CONTEXT_SECONDS), min(duration, end + ASR_CONTEXT_SECONDS)
    return AsrWindow(left, right, (start - left, end - left))


def shift_tail(tail, offset):
    """Shift the private last-word checkpoint; never expose it as a sentence."""
    if tail is None:
        return None
    return {**tail, "start_seconds": round(tail["start_seconds"] + offset, 3),
            "end_seconds": round(tail["end_seconds"] + offset, 3)}


def same_boundary_word(word, tail):
    """Require the same word AND substantial temporal agreement, not text alone."""
    if not tail or not word["text"].strip() or word["text"].strip().casefold() != tail["text"].strip().casefold():
        return False
    start, end = word["begin_time"] / 1000, word["end_time"] / 1000
    other_start, other_end = tail["start_seconds"], tail["end_seconds"]
    short, long = sorted((end - start, other_end - other_start))
    return (short > 0 and long <= 2 * short
            and min(end, other_end) - max(start, other_start) > short / 2
            and abs((start + end - other_start - other_end) / 2) < short / 2)


def append_transcript(sentences, rows, offset):
    """Join an unfinished boundary clause; keep original provider range endpoints."""
    for index, row in enumerate(rows):
        current = {"id": len(sentences) + 1, "start_seconds": round(offset + row["start_seconds"], 3),
                   "end_seconds": round(offset + row["end_seconds"], 3), "text": row["text"]}
        if index == 0 and sentences:
            previous = sentences[-1]
            gap = current["start_seconds"] - previous["end_seconds"]
            if (not re.search(r"[。！？.!?;；,，:：]$", previous["text"])
                    and -.5 <= gap <= .5 and current["end_seconds"] - previous["start_seconds"] <= 20):
                separator = " " if re.search(r"[A-Za-z0-9]$", previous["text"]) and re.match(r"[A-Za-z0-9]", current["text"]) else ""
                previous["text"] += separator + current["text"]
                previous["end_seconds"] = max(previous["end_seconds"], current["end_seconds"])
                continue
        sentences.append(current)
