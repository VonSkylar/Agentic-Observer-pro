"""Public request text and validated, night-scoped operating instructions.

Only stdin data is used. The model interprets language/time zones; the agent
enforces UTC boundaries and executes the resulting schedule deterministically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import re
import unicodedata

INSTRUMENT_TEXT = re.compile(r"导星|導星|相机|相機|カメラ|平场|平場|镜盖|鏡蓋|フラット|guider|flat.?lamp|mirror.?cover", re.I)
LONG_TEXT = int(os.environ.get("PRO_HANDOVER_LONG_TEXT", "200"))


def source_dates(text):
    text = unicodedata.normalize("NFKC", text)
    return {(int(m), int(d)) for m, d in re.findall(r"(?<!\d)(\d{1,2})\s*(?:/|月)\s*(\d{1,2})(?!\d)", text)}


def utc(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timestamp must include a time zone")
    return moment.astimezone(timezone.utc)


class Handover:
    def __init__(self, site: dict):
        self.site = {k: site[k] for k in ("utc_offset_hours", "latitude_deg", "longitude_deg") if k in site}
        self.records = {}
        self.operations = []
        self.attempted_faults = set()
        self.repaired_through = None

    def ingest(self, requests):
        for request in requests:
            if not isinstance(request, dict):
                continue
            text, rid = request.get("reason"), request.get("request_id")
            if isinstance(text, str) and text.strip() and isinstance(rid, str):
                self.records[rid] = {k: request[k] for k in
                                     ("request_id", "issued_at_utc", "deadline_utc", "reason") if k in request}

    def context(self, start, end):
        # Keep the first long handover's decoding conventions even after it expires.
        # Include unexpired sources and the last expired handover as continuity.
        records = list(self.records.values())
        long = [r for r in records if len(r["reason"]) > LONG_TEXT]
        seed = long[:1]
        active, expired = [], []
        for record in records:
            try:
                issued = utc(record["issued_at_utc"])
                deadline = utc(record["deadline_utc"])
            except (KeyError, ValueError, TypeError, OverflowError):
                active.append(record)  # missing metadata must not silently drop public text
                continue
            if issued > end:
                continue
            (active if deadline >= start else expired).append(record)
        continuity = sorted((r for r in expired if len(r["reason"]) > LONG_TEXT),
                            key=lambda r: r.get("issued_at_utc", ""))[-1:]
        chosen = {r["request_id"]: r for r in seed + continuity + active}
        texts = []
        for record in chosen.values():
            # Number the original lines so the model can cite a source without
            # retyping multilingual text (or silently changing its character set).
            lines = record["reason"].replace("\\n", "\n").split("\n")
            texts.append({**{k: v for k, v in record.items() if k != "reason"},
                          "reason_lines": [{"line": i, "text": text} for i, text in enumerate(lines, 1)]})
        context = {"site": self.site, "night_start_utc": start.isoformat(), "night_end_utc": end.isoformat(),
                   "request_texts": texts}
        context["instrument_candidates"] = [
            {"source_request_id": record["request_id"], "source_line": row["line"], "text": row["text"]}
            for record in texts for row in record["reason_lines"] if INSTRUMENT_TEXT.search(row["text"])]
        if "utc_offset_hours" in self.site:
            offset = timedelta(hours=float(self.site["utc_offset_hours"]))
            context["local_night_start"] = (start + offset).replace(tzinfo=None).isoformat()
            context["local_night_end"] = (end + offset).replace(tzinfo=None).isoformat()
        relevant_dates = {(moment.month, moment.day) for moment in (start, end, start + timedelta(hours=9),
                          end + timedelta(hours=9), start + timedelta(hours=float(self.site.get("utc_offset_hours", 0))),
                          end + timedelta(hours=float(self.site.get("utc_offset_hours", 0))))}
        context["instrument_candidates"] = [row for row in context["instrument_candidates"]
                                            if not source_dates(row["text"]) or source_dates(row["text"]) & relevant_dates]
        context["relevant_source_dates"] = sorted(relevant_dates)
        return context

    @staticmethod
    def night_context(context):
        """Focus the plan on nearby dates; full text still goes to fault review.

        Keep undated conventions/corrections and stable ORIGINAL line numbers.
        No semantic interpretation or card-specific facts are introduced here.
        """
        relevant = {tuple(date) for date in context.get("relevant_source_dates", [])}
        texts = []
        for record in context.get("request_texts", []):
            lines = [row for row in record["reason_lines"] if not source_dates(row["text"])
                     or source_dates(row["text"]) & relevant]
            if lines:
                texts.append({**record, "reason_lines": lines})
        return {**context, "request_texts": texts}

    @staticmethod
    def validate(operations, context):
        if not isinstance(operations, list):
            return []
        sources = {r["request_id"]: r["reason_lines"] for r in context.get("request_texts", [])}
        start, end = utc(context["night_start_utc"]), utc(context["night_end_utc"])
        result = {}
        for op in operations:
            if not isinstance(op, dict) or op.get("kind") not in ("report_fault", "test_window"):
                continue
            source, quote = op.get("source_request_id"), op.get("evidence")
            if not isinstance(source, str) or source not in sources:
                continue
            line = op.get("source_line")
            if line is not None:
                if type(line) is not int or not 1 <= line <= len(sources[source]):
                    continue
                quote = sources[source][line - 1]["text"]
                if not quote.strip():
                    continue
            elif (not isinstance(quote, str) or not quote.strip()
                  or quote not in "\n".join(row["text"] for row in sources[source])):
                continue
            try:
                if "start_time" in op:
                    offset = op.get("utc_offset_hours")
                    if type(offset) not in (int, float) or not math.isfinite(offset) or not -12 <= offset <= 14:
                        continue
                    tz = timezone(timedelta(hours=offset))
                    at = datetime.fromisoformat(op["start_time"])
                    until = datetime.fromisoformat(op["end_time"]) if op["kind"] == "test_window" else at
                    if at.tzinfo is not None or until.tzinfo is not None:
                        continue
                    dates = source_dates(quote)
                    if dates and (at.month, at.day) not in dates:
                        # Test clocks after midnight belong to the next calendar
                        # date of the source's observing night, never a new night.
                        preceding = at - timedelta(days=1)
                        if not (op["kind"] == "test_window" and at.hour < 12
                                and (preceding.month, preceding.day) in dates):
                            continue
                    at, until = at.replace(tzinfo=tz).astimezone(timezone.utc), until.replace(tzinfo=tz).astimezone(timezone.utc)
                else:  # retain compatibility with direct UTC answers
                    at = utc(op.get("start_utc"))
                    until = utc(op.get("end_utc")) if op["kind"] == "test_window" else at
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            if op["kind"] == "report_fault":
                if not start <= at < end:
                    continue
            elif not (at < until and at < end and until > start):
                continue
            else:
                at, until = max(at, start), min(until, end)
            result[(op["kind"], at, until)] = {"kind": op["kind"], "start": at, "end": until,
                                               "source_request_id": source, "source_line": line}
        return sorted(result.values(), key=lambda op: (op["start"], op["kind"]))

    def apply(self, operations):
        self.operations = operations

    def test_end(self, now):
        ends = [op["end"] for op in self.operations if op["kind"] == "test_window"
                and op["start"] <= now < op["end"]]
        return max(ends) if ends else None

    def due_fault(self, now):
        for op in self.operations:
            if (op["kind"] == "report_fault" and op["start"] <= now
                    and op["start"] not in self.attempted_faults
                    and (self.repaired_through is None or op["start"] > self.repaired_through)):
                return op
        return None

    def next_boundary(self, now, end):
        return min([end] + [op["start"] for op in self.operations if now < op["start"] < end])
