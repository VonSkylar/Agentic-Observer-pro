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
CORRECTION_LOOKBACK = int(os.environ.get("PRO_CORRECTION_LOOKBACK", "3"))
DIRECTIONS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
CLOCK_TEXT = re.compile(r"(?:\d|[零一二三四五六七八九十])\s*[:：时時点點]|\b(?:do|re|mi|fa|sol|la|si)\b", re.I)
CORRECTION_TEXT = re.compile(r"更正|訂正|订正|旧图|舊圖|以此为准|以此為準|correction|supersede", re.I)


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


def declared_clock_hint(text, conventions):
    """Decode clock digits only when the PUBLIC text supplies their mapping.

    This is a lexical aid for the model, not a fault/time decision. Original
    source lines remain unchanged and are still mandatory for validation.
    """
    mapping = {k.lower(): v for k, v in re.findall(r"(高音\s*(?:do|re)|do|re|mi|fa|sol|la|si|休止)\s*=\s*([0-9])", conventions, re.I)}
    if not mapping:
        return None
    keys = sorted(mapping, key=len, reverse=True)
    atom = "(?:" + "|".join(re.escape(k) for k in keys) + ")"
    pattern = re.compile(r"(?<![A-Za-z])(" + atom + r"(?:\s+" + atom + r")*)\s*[:：]\s*(" + atom + r"(?:\s+" + atom + r")*)(?![A-Za-z])", re.I)
    def digits(part):
        return "".join(mapping[token.lower()] for token in re.findall(atom, part, re.I))
    clocks = [digits(m[1]) + ":" + digits(m[2]) for m in pattern.finditer(text)]
    return clocks or None


def explicit_weather_clocks(text, year):
    """Read a two-clock numeric source span without asking a model to copy digits.

    The model still chooses the authoritative corrected line and its meaning.
    Ambiguous/word-based spans retain the normal model-and-validator path.
    """
    text = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    text = re.sub(r"(\d{1,2})[时時](\d{1,2})分?", r"\1:\2", text)
    clocks = list(re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text))
    dates = list(re.finditer(r"(?<!\d)(\d{1,2})(?:/|月)(\d{1,2})(?!\d)", text))
    if len(clocks) != 2 or not dates or dates[0].start() > clocks[0].start():
        return None
    moments, used_dates = [], []
    try:
        for clock in clocks:
            date = [d for d in dates if d.start() < clock.start()][-1]
            moments.append(datetime(year, int(date[1]), int(date[2]), int(clock[1]), int(clock[2])))
            used_dates.append(date.start())
        if moments[1] <= moments[0]:
            if used_dates[0] == used_dates[1]:
                moments[1] += timedelta(days=1)
            elif moments[0].month == 12 and moments[1].month == 1:
                moments[1] = moments[1].replace(year=year + 1)
            else:
                return None
    except ValueError:
        return None
    return moments


class Handover:
    def __init__(self, site: dict):
        self.site = {k: site[k] for k in ("utc_offset_hours", "latitude_deg", "longitude_deg") if k in site}
        self.records = {}
        self.operations = []
        self.attempted_faults = set()
        self.repaired_through = None
        self.terrain = {}
        self.weather = []

    def ingest(self, requests):
        for request in requests:
            if not isinstance(request, dict):
                continue
            text, rid = request.get("reason"), request.get("request_id")
            if isinstance(text, str) and text.strip() and isinstance(rid, str):
                self.terrain = {d: item for d, item in self.terrain.items()
                                if item["source_request_id"] != rid or item.get("evidence", "") in text}
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
                   "request_texts": texts,
                   "known_terrain": [{k: v for k, v in item.items() if k != "evidence"}
                                     for item in self.terrain.values()]}
        context["instrument_candidates"] = [
            {"source_request_id": record["request_id"], "source_line": row["line"], "text": row["text"]}
            for record in texts for row in record["reason_lines"] if INSTRUMENT_TEXT.search(row["text"])]
        conventions = "\n".join(row["text"] for record in texts for row in record["reason_lines"]
                                if re.search(r"\bdo\s*=", row["text"], re.I))
        for row in context["instrument_candidates"]:
            hint = declared_clock_hint(row["text"], conventions)
            if hint:
                row["declared_clock_digits"] = hint
            dates = source_dates(row["text"])
            confirmations = [{"source_line": other["line"], "text": other["text"]}
                             for record in texts if record["request_id"] == row["source_request_id"]
                             for other in record["reason_lines"] if other["line"] != row["source_line"]
                             and dates & source_dates(other["text"])
                             and re.search(r"所以|因此|确认|確認|下雨了|confirmed|rained", other["text"], re.I)
                             and re.search(r"顺延|順延|postpon|delay", other["text"], re.I)]
            if confirmations:
                row["postponement_confirmations"] = confirmations
        if "utc_offset_hours" in self.site:
            offset = timedelta(hours=float(self.site["utc_offset_hours"]))
            context["local_night_start"] = (start + offset).replace(tzinfo=None).isoformat()
            context["local_night_end"] = (end + offset).replace(tzinfo=None).isoformat()
            context["local_observing_date"] = context["local_night_start"][:10]
            local_date = start + offset
            context["local_test_sources"] = [
                {"source_request_id": row["source_request_id"], "source_line": row["source_line"]}
                for row in context["instrument_candidates"]
                if (local_date.month, local_date.day) in source_dates(row["text"])
                and re.search(r"平场|平場|镜盖|鏡蓋|フラット|flat.?lamp|mirror.?cover", row["text"], re.I)]
        relevant_dates = {(moment.month, moment.day) for moment in (start, end, start + timedelta(hours=9),
                          end + timedelta(hours=9), start + timedelta(hours=float(self.site.get("utc_offset_hours", 0))),
                          end + timedelta(hours=float(self.site.get("utc_offset_hours", 0))))}
        # A confirmed one-day postponement can refer to the PREVIOUS local night.
        previous_local = start + timedelta(hours=float(self.site.get("utc_offset_hours", 0))) - timedelta(days=1)
        relevant_dates.add((previous_local.month, previous_local.day))
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
            def relevant_line(row):
                dates = source_dates(row["text"])
                return (not dates or bool(dates & relevant)
                        or len(dates) > 1 and any(min(dates) <= day <= max(dates) for day in relevant))
            lines = [row for row in record["reason_lines"] if relevant_line(row)]
            if lines:
                texts.append({**record, "reason_lines": lines})
        return {**context, "request_texts": texts}

    @staticmethod
    def model_context(context):
        """Put reusable source text before changing clocks for provider prefix caching.

        Keep candidate quotes as well as original numbered lines: the quotes help
        the model attend to multilingual instrument records. No evidence is removed.
        """
        return {"site": context.get("site", {}), "request_texts": context.get("request_texts", []),
                **{k: v for k, v in context.items() if k not in ("site", "request_texts", "instrument_candidates")},
                "instrument_candidates": context.get("instrument_candidates", [])}

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
                if type(line) is not int:
                    continue
                quote = next((row["text"] for row in sources[source] if row["line"] == line), None)
                if not quote or not quote.strip():
                    continue
            elif (not isinstance(quote, str) or not quote.strip()
                  or quote not in "\n".join(row["text"] for row in sources[source])):
                continue
            support = op.get("source_lines", [])
            if not isinstance(support, list) or any(type(n) is not int for n in support):
                continue
            support_quotes = [row["text"] for row in sources[source] if row["line"] in support]
            if len(support_quotes) != len(set(support)):
                continue
            try:
                if "start_time" in op:
                    zone = op.get("time_zone")
                    if zone is not None and (not isinstance(zone, str) or zone not in ("local", "UTC", "Tokyo")):
                        continue
                    offset = ({"local": context.get("site", {}).get("utc_offset_hours"), "UTC": 0, "Tokyo": 9}[zone]
                              if zone is not None else op.get("utc_offset_hours"))
                    if type(offset) not in (int, float) or not math.isfinite(offset) or not -12 <= offset <= 14:
                        continue
                    tz = timezone(timedelta(hours=offset))
                    at = datetime.fromisoformat(op["start_time"])
                    until = datetime.fromisoformat(op["end_time"]) if op["kind"] == "test_window" else at
                    if at.tzinfo is not None or until.tzinfo is not None:
                        continue
                    dates = source_dates(quote)
                    # After-midnight tests belong to the next calendar day of an
                    # explicitly named observing night, never that day's morning.
                    if (op["kind"] == "test_window" and at.hour < 12 and (at.month, at.day) in dates
                            and re.search(r"当晚|當晚|夜里|夜裡|夜の|night|晚上", quote, re.I)):
                        continue
                    if dates and (at.month, at.day) not in dates:
                        # Test clocks after midnight belong to the next calendar
                        # date of the source's observing night, never a new night.
                        preceding = at - timedelta(days=1)
                        postponed = any(re.search(r"(?:所以|因此|确认|確認|下雨了|confirmed|rained)", q, re.I)
                                        and re.search(r"(?:顺延|順延|往后|往後|延期|postpon|delay).{0,15}(?:一天|一日|1日|1 day|one day)", q, re.I)
                                        for q in support_quotes if q != quote)
                        if not ((op["kind"] == "test_window" and at.hour < 12 or postponed)
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
                                               "source_request_id": source, "source_line": line,
                                               "source_lines": support}
        return sorted(result.values(), key=lambda op: (op["start"], op["kind"]))

    @staticmethod
    def validate_environment(answer, context):
        """Accept only cited, bounded facts; clocks use the same validation as tests.

        Language, corrections and direction ranges are interpreted by the advisor.
        Numeric terrain heights must also occur in the quoted line (or be the
        complement of an explicitly stated zenith distance).
        """
        sources = {r["request_id"]: {row["line"]: row["text"] for row in r["reason_lines"]}
                   for r in context.get("request_texts", [])}
        superseded = set()
        for rid, lines in sources.items():
            last_clock = None
            for number, text in sorted(lines.items()):
                if INSTRUMENT_TEXT.search(text) or not CLOCK_TEXT.search(unicodedata.normalize("NFKC", text)):
                    continue
                if CORRECTION_TEXT.search(text) and last_clock is not None and number - last_clock <= CORRECTION_LOOKBACK:
                    superseded.add((rid, last_clock))
                last_clock = number
        terrain, weather = [], []
        entries = answer.get("terrain", [])
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict):
                continue
            source, line = item.get("source_request_id"), item.get("source_line")
            if not isinstance(source, str) or type(line) is not int:
                continue
            quote = sources.get(source, {}).get(line, "")
            altitude = item.get("altitude_deg")
            directions = item.get("directions")
            if (not quote or type(altitude) not in (int, float) or not math.isfinite(altitude)
                    or not 0 <= altitude <= 85 or not isinstance(directions, list)
                    or not directions or any(not isinstance(d, str) or d not in DIRECTIONS for d in directions)):
                continue
            numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", quote))]
            zenith = re.search(r"天顶距|天頂距|zenith", quote, re.I)
            if not any(abs(altitude - n) < 0.01 or (zenith and abs(altitude - (90 - n)) < 0.01) for n in numbers):
                continue
            for direction in sorted(set(directions)):
                terrain.append({"direction": direction, "altitude_deg": float(altitude),
                                "source_request_id": source, "source_line": line, "evidence": quote})
        entries = answer.get("weather", [])
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict) or item.get("effect") not in ("closed", "thin_cloud"):
                continue
            directions = item.get("directions")
            if (not isinstance(directions, list) or not directions
                    or any(not isinstance(d, str) or d not in DIRECTIONS | {"ALL"} for d in directions)):
                continue
            source, line = item.get("source_request_id"), item.get("source_line")
            if not isinstance(source, str) or type(line) is not int:
                continue
            quote = sources.get(source, {}).get(line, "")
            # Conventions and untimed continuations cannot justify an invented interval.
            if (source, line) in superseded or not CLOCK_TEXT.search(unicodedata.normalize("NFKC", quote)):
                continue
            # Anchor an undated year to the source publication, not the current
            # night: a retained decoding legend must not revive last year's rain.
            record = next(r for r in context["request_texts"] if r["request_id"] == source)
            anchor = utc(record.get("issued_at_utc", context["night_start_utc"]))
            explicit = explicit_weather_clocks(quote, anchor.year)
            if explicit and explicit[0].month < anchor.month - 6:
                explicit = explicit_weather_clocks(quote, anchor.year + 1)
            elif explicit and explicit[0].month > anchor.month + 6:
                explicit = explicit_weather_clocks(quote, anchor.year - 1)
            if explicit is not None:
                # Use the actual cited clock span, including both explicit dates.
                # Fixing a copied digit cannot change the model-selected meaning.
                item = {**item, "start_time": explicit[0].isoformat(), "end_time": explicit[1].isoformat()}
            valid = Handover.validate([{**item, "kind": "test_window"}], context)
            if valid:
                weather.append({**valid[0], "kind": "weather", "effect": item["effect"],
                                "directions": sorted(set(directions))})
        return terrain, weather

    def apply_environment(self, terrain, weather):
        # Conflicting limits in one answer use the higher safe horizon. A later
        # authoritative answer can still replace a previous night's limit.
        by_direction = {}
        for item in terrain:
            old = by_direction.get(item["direction"])
            if old is None or item["altitude_deg"] > old["altitude_deg"]:
                by_direction[item["direction"]] = item
        for item in by_direction.values():
            self.terrain[item["direction"]] = item
        self.weather = weather

    def apply(self, operations):
        self.operations = operations

    def add_operations(self, operations):
        merged = {(op["kind"], op["start"], op["end"]): op for op in self.operations + operations}
        self.operations = sorted(merged.values(), key=lambda op: (op["start"], op["kind"]))

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
        return min([end] + [op["start"] for op in self.operations if now < op["start"] < end]
                   + [op[key] for op in self.weather for key in ("start", "end") if now < op[key] < end])
