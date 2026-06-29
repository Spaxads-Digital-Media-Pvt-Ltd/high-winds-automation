"""
utils/lead_pacer.py — Smart lead-pacing scheduler.

Distributes a total number of leads across a USA operating window with
peak-hour throttling, clamped to the physical processing capacity of the
funnels.  A token-bucket-style release gate keeps the pipeline full without
bunching, accounting for real per-lead processing time.

This module is pure logic + thread-safe runtime state. It does NOT touch
Playwright, Flask, or the sheet — the engine threads call into it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ── Config dataclasses ──────────────────────────────────────────────────────

@dataclass
class ProcessingConfig:
    avg_success_time_min: float = 1.5
    avg_retry_time_min: float = 3.0
    assumed_success_rate: float = 0.80
    max_retries: int = 1

    @property
    def realistic_time_per_lead(self) -> float:
        # Weighted average: success_rate take the fast path, the rest retry.
        return (
            self.assumed_success_rate * self.avg_success_time_min
            + (1 - self.assumed_success_rate) * self.avg_retry_time_min
        )

    @property
    def max_leads_per_hour_per_offer(self) -> int:
        return max(1, int(60 / self.realistic_time_per_lead))


@dataclass
class HourlySlot:
    hour_start: datetime
    hour_end: datetime
    offer_budgets: dict          # offer_id -> planned count
    offer_actuals: dict          # offer_id -> completed count (runtime)
    offer_released: dict         # offer_id -> released count (runtime, internal)
    total_planned: int
    total_actual: int
    is_peak: bool
    capacity_pct: float          # total_planned / (realistic_max * num_offers) * 100
    fraction: float = 1.0        # proration for a partial first/last hour


# ── Distribution helper ─────────────────────────────────────────────────────

def _distribute(total: int, weights: list[float], cap: int) -> list[int]:
    """Distribute ``total`` units across slots proportional to ``weights``,
    with each slot capped at ``cap``. Uses iterative water-filling + largest
    remainder so the result is integer and sums to min(total, cap*active)."""
    n = len(weights)
    res = [0] * n
    remaining = total
    for _ in range(n + 5):  # converges well within this many passes
        active = [i for i in range(n) if weights[i] > 0 and res[i] < cap]
        if remaining <= 0 or not active:
            break
        wsum = sum(weights[i] for i in active)
        if wsum <= 0:
            break
        assigned = 0
        fracs: list[tuple[float, int]] = []
        for i in active:
            ideal = weights[i] / wsum * remaining
            give = min(int(ideal), cap - res[i])
            if give > 0:
                res[i] += give
                assigned += give
            fracs.append((ideal - int(ideal), i))
        remaining -= assigned
        fracs.sort(reverse=True)
        for _frac, i in fracs:
            if remaining <= 0:
                break
            if res[i] < cap and weights[i] > 0:
                res[i] += 1
                remaining -= 1
        if assigned == 0 and remaining > 0:
            # only fractional rounding left and nothing took — stop.
            break
    return res


# ── The pacer ───────────────────────────────────────────────────────────────

class LeadPacer:
    def __init__(
        self,
        offer_leads: dict[str, int],
        start_time: datetime,
        end_time: datetime,
        peak_hours_config: list[dict],
        processing_config: ProcessingConfig,
        stagger_minutes: int = 12,
        catch_up_threshold: float = 0.30,
        off_peak_multiplier: float = 1.0,
        tz_name: str = "America/New_York",
        offer_names: Optional[dict] = None,
    ) -> None:
        # Per-offer lead totals are the source of truth — each offer gets its
        # own count, distributed across the window independently.
        self.offer_leads = {o: int(n) for o, n in offer_leads.items()}
        self.offers = list(self.offer_leads.keys())
        self.total_leads = sum(self.offer_leads.values())
        self.offer_names = offer_names or {o: o for o in self.offers}
        self.peak_cfg = peak_hours_config or []
        self.proc = processing_config
        self.stagger_minutes = stagger_minutes
        self.catch_up_threshold = catch_up_threshold
        self.off_peak_multiplier = off_peak_multiplier
        self.tz = ZoneInfo(tz_name) if ZoneInfo else None
        self.start_time = self._aware(start_time)
        self.end_time = self._aware(end_time)

        self._lock = threading.RLock()
        self._recent_durations: list[float] = []   # rolling window (seconds)
        self.observed_time_per_lead: Optional[float] = None
        self.drift_warning: Optional[str] = None

        self.slots: list[HourlySlot] = self.generate_plan()

    # ── time helpers ────────────────────────────────────────────────────────

    def _aware(self, dt: datetime) -> datetime:
        if dt.tzinfo is None and self.tz is not None:
            return dt.replace(tzinfo=self.tz)
        return dt

    def _now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else datetime.now()

    @property
    def realistic_max(self) -> int:
        return self.proc.max_leads_per_hour_per_offer

    @property
    def num_offers(self) -> int:
        return max(1, len(self.offers))

    def _total_hours(self) -> float:
        return max(0.0, (self.end_time - self.start_time).total_seconds() / 3600.0)

    @staticmethod
    def _parse_hhmm(s: str) -> dtime:
        h, m = (s.strip().split(":") + ["0"])[:2]
        return dtime(int(h), int(m))

    def _peak_for(self, dt: datetime) -> tuple[bool, float]:
        """Return (is_peak, multiplier) for the clock time of ``dt``."""
        t = dt.timetz().replace(tzinfo=None)
        best_mult = None
        for w in self.peak_cfg:
            try:
                ws, we = self._parse_hhmm(w["start"]), self._parse_hhmm(w["end"])
                mult = float(w.get("multiplier", 0.65))
            except Exception:
                continue
            in_window = (ws <= t < we) if ws <= we else (t >= ws or t < we)
            if in_window:
                best_mult = mult if best_mult is None else min(best_mult, mult)
        if best_mult is not None:
            return True, best_mult
        return False, self.off_peak_multiplier

    # ── raw slot scaffolding ─────────────────────────────────────────────────

    def _build_raw_slots(self) -> list[dict]:
        slots: list[dict] = []
        cur = self.start_time
        while cur < self.end_time:
            boundary = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            seg_end = min(boundary, self.end_time)
            fraction = (seg_end - cur).total_seconds() / 3600.0
            is_peak, mult = self._peak_for(cur)
            slots.append({
                "start": cur, "end": seg_end, "fraction": fraction,
                "is_peak": is_peak, "mult": mult,
            })
            cur = seg_end
        return slots

    # ── plan generation (spec steps 1-7) ─────────────────────────────────────

    def generate_plan(self, offer_leads_override: Optional[dict[str, int]] = None,
                      start_override: Optional[datetime] = None) -> list[HourlySlot]:
        leads = self.offer_leads if offer_leads_override is None else offer_leads_override
        if start_override is not None:
            self.start_time = self._aware(start_override)
        raw = self._build_raw_slots()
        cap = self.realistic_max

        # Weight each slot by peak multiplier * proration fraction.
        weights = [s["mult"] * s["fraction"] for s in raw]

        # Distribute each offer's own lead count independently across slots,
        # clamped to cap. Peak reduction + redistribution of surplus to off-peak
        # hours are handled by the weighted water-fill.
        offer_alloc: dict[str, list[int]] = {}
        for offer_id in self.offers:
            offer_alloc[offer_id] = _distribute(int(leads.get(offer_id, 0)), weights, cap)

        slots: list[HourlySlot] = []
        for si, s in enumerate(raw):
            budgets = {o: offer_alloc[o][si] for o in self.offers}
            total_planned = sum(budgets.values())
            cap_total = cap * self.num_offers
            slots.append(HourlySlot(
                hour_start=s["start"], hour_end=s["end"],
                offer_budgets=budgets,
                offer_actuals={o: 0 for o in self.offers},
                offer_released={o: 0 for o in self.offers},
                total_planned=total_planned, total_actual=0,
                is_peak=s["is_peak"],
                capacity_pct=round(total_planned / cap_total * 100, 1) if cap_total else 0.0,
                fraction=round(s["fraction"], 3),
            ))
        return slots

    # ── feasibility (spec step 2) ─────────────────────────────────────────────

    def validate_feasibility(self) -> tuple[bool, str]:
        hours = self._total_hours()
        if hours <= 0:
            return False, "End time must be after start time."
        # Each offer runs in its own thread, so the binding constraint is the
        # busiest single offer vs the per-offer capacity (not the combined total).
        cap_per_offer = self.realistic_max
        busiest = max(self.offer_leads.items(), key=lambda kv: kv[1], default=(None, 0))
        busiest_id, busiest_n = busiest
        req_rate = busiest_n / hours
        if req_rate > cap_per_offer:
            name = self.offer_names.get(busiest_id, busiest_id)
            return False, (
                f"⚠️ {name} needs {req_rate:.1f} leads/hr but max realistic capacity is "
                f"~{cap_per_offer}/hr/offer. Reduce its leads or extend the window."
            )
        return True, (
            f"Feasible — busiest offer needs {req_rate:.1f}/hr vs {cap_per_offer}/hr "
            f"per-offer capacity. Total {self.total_leads} leads over {hours:.1f}h."
        )

    # ── runtime gate (token bucket) ───────────────────────────────────────────

    def _slot_index_for(self, dt: datetime) -> Optional[int]:
        for i, s in enumerate(self.slots):
            if s.hour_start <= dt < s.hour_end:
                return i
        return None

    def _stagger_offset_seconds(self, offer_id: str) -> float:
        try:
            idx = self.offers.index(offer_id)
        except ValueError:
            return 0.0
        return min(idx * self.stagger_minutes * 60, 1800)  # cap at 30 min

    def _future_budget(self, offer_id: str, from_idx: int) -> int:
        return sum(
            self.slots[j].offer_budgets.get(offer_id, 0)
            for j in range(from_idx + 1, len(self.slots))
        )

    def _total_released(self, offer_id: str) -> int:
        return sum(s.offer_released.get(offer_id, 0) for s in self.slots)

    def get_budget_for_hour(self, offer_id: str, dt: datetime) -> int:
        with self._lock:
            idx = self._slot_index_for(self._aware(dt))
            return self.slots[idx].offer_budgets.get(offer_id, 0) if idx is not None else 0

    def get_release_interval(self, offer_id: str, dt: datetime) -> float:
        """Seconds between releases within the current hour for this offer."""
        with self._lock:
            idx = self._slot_index_for(self._aware(dt))
            if idx is None:
                return 0.0
            budget = self.slots[idx].offer_budgets.get(offer_id, 0)
            if budget <= 0:
                return 0.0
            offset = self._stagger_offset_seconds(offer_id)
            return (3600.0 - offset) / budget

    def get_wait_seconds(self, offer_id: str, dt: Optional[datetime] = None) -> Optional[float]:
        """Seconds to wait before releasing this offer's next lead.

        Auto-paces against the plan's cumulative-release curve:
          • behind  (released < what the plan says should be out by now) → 0 → release
                     immediately so the offer catches up (pace up);
          • ahead/on-track → wait until the curve reaches released+1 (slow down).
        This guarantees the offer's defined leads are filled by ``end_time``.
        Returns None when this offer is finished or the window has closed."""
        now = self._aware(dt) if dt else self._now()
        with self._lock:
            target_total = self.offer_leads.get(offer_id, 0)
            released = self._total_released(offer_id)
            if released >= target_total:
                return None  # this offer is done
            if now >= self.end_time:
                return None
            if now < self.start_time:
                return (self.start_time - now).total_seconds()
            idx = self._slot_index_for(now)
            if idx is None:
                return None
            slot = self.slots[idx]
            budget = slot.offer_budgets.get(offer_id, 0)
            cum_before = sum(self.slots[j].offer_budgets.get(offer_id, 0) for j in range(idx))
            slot_secs = (slot.hour_end - slot.hour_start).total_seconds()
            elapsed = (now - slot.hour_start).total_seconds()
            # How many leads the plan says should already be released by `now`.
            target_now = cum_before + (budget * (elapsed / slot_secs) if slot_secs > 0 else 0)
            if released < target_now:
                return 0.0  # behind → pace up, release now
            # On track / ahead → time until the curve reaches the next lead.
            needed_in_hour = (released + 1) - cum_before
            if budget > 0 and needed_in_hour <= budget:
                t_due = slot.hour_start + timedelta(seconds=(needed_in_hour / budget) * slot_secs)
                return max(0.0, (t_due - now).total_seconds())
            # Current hour is exhausted for this offer; wait for the next hour
            # that still carries budget.
            if self._future_budget(offer_id, idx) > 0:
                return max(0.0, (slot.hour_end - now).total_seconds())
            # No future budget but offer still owes leads (plan was clamped /
            # infeasible) → release the remainder now to honour the defined total.
            return 0.0

    def can_proceed(self, offer_id: str, dt: Optional[datetime] = None) -> bool:
        return self.get_wait_seconds(offer_id, dt) is not None

    def consume(self, offer_id: str, dt: Optional[datetime] = None) -> datetime:
        """Mark one lead released for the current hour. Returns the release time."""
        now = self._aware(dt) if dt else self._now()
        with self._lock:
            idx = self._slot_index_for(now)
            if idx is not None:
                slot = self.slots[idx]
                slot.offer_released[offer_id] = slot.offer_released.get(offer_id, 0) + 1
        return now

    def record_submission(self, offer_id: str, release_dt: datetime,
                          success: bool, duration_seconds: Optional[float] = None) -> None:
        """Record a completed lead against the hour it was RELEASED in (keeps the
        release gate clean for in-flight leads spanning an hour boundary)."""
        with self._lock:
            idx = self._slot_index_for(self._aware(release_dt))
            if idx is not None:
                slot = self.slots[idx]
                slot.offer_actuals[offer_id] = slot.offer_actuals.get(offer_id, 0) + 1
                slot.total_actual = sum(slot.offer_actuals.values())
            if duration_seconds and duration_seconds > 0:
                self._recent_durations.append(duration_seconds)
                self._recent_durations = self._recent_durations[-20:]
                avg = sum(self._recent_durations) / len(self._recent_durations)
                self.observed_time_per_lead = avg / 60.0  # minutes
                configured = self.proc.realistic_time_per_lead
                if self.observed_time_per_lead > configured * 1.4:
                    self.drift_warning = (
                        f"⚠️ Avg processing time increased to "
                        f"{self.observed_time_per_lead:.1f} min "
                        f"(configured: {configured:.1f} min). Pacing may fall behind."
                    )
                else:
                    self.drift_warning = None

    # ── recompute after skip / restart (spec) ─────────────────────────────────

    def recalculate_remaining(self, from_time: Optional[datetime] = None) -> list[HourlySlot]:
        """Redistribute each offer's still-unreleased leads across the remaining
        window, preserving what's already been released this run."""
        now = self._aware(from_time) if from_time else self._now()
        with self._lock:
            remaining = {
                o: max(0, self.offer_leads.get(o, 0) - self._total_released(o))
                for o in self.offers
            }
            # Preserve completed/started hours; only rebuild from `now` forward.
            self.start_time = max(self.start_time, now)
            new_slots = self.generate_plan(offer_leads_override=remaining,
                                           start_override=self.start_time)
            # Keep past slots (with their actuals) and append the new future plan.
            past = [s for s in self.slots if s.hour_end <= now]
            self.slots = past + new_slots
            return self.slots

    # ── serialization for the UI ──────────────────────────────────────────────

    def _slot_label(self, s: HourlySlot) -> str:
        a = s.hour_start.strftime("%-I:%M") if hasattr(s.hour_start, "strftime") else ""
        b = s.hour_end.strftime("%-I:%M %p")
        return f"{a}-{b}"

    def plan_dict(self) -> list[dict]:
        with self._lock:
            out = []
            for i, s in enumerate(self.slots):
                out.append({
                    "index": i + 1,
                    "label": self._slot_label(s),
                    "hour_start": s.hour_start.isoformat(),
                    "offer_budgets": dict(s.offer_budgets),
                    "total_planned": s.total_planned,
                    "is_peak": s.is_peak,
                    "capacity_pct": s.capacity_pct,
                })
            return out

    def get_pacing_stats(self) -> dict:
        now = self._now()
        with self._lock:
            cur_idx = self._slot_index_for(now)
            rows = []
            completed = 0
            planned_total = 0
            for i, s in enumerate(self.slots):
                planned_total += s.total_planned
                completed += s.total_actual
                if cur_idx is not None and i < cur_idx:
                    state = "past"
                elif i == cur_idx:
                    state = "current"
                else:
                    state = "future"
                # accuracy for completed/current hours
                status = "—"
                if state in ("past", "current") and s.total_planned > 0:
                    ratio = s.total_actual / s.total_planned
                    if ratio >= 0.9:
                        status = "on_track"
                    elif ratio >= 0.7:
                        status = "behind"
                    else:
                        status = "way_behind"
                rows.append({
                    "index": i + 1,
                    "label": self._slot_label(s),
                    "offer_budgets": dict(s.offer_budgets),
                    "offer_actuals": dict(s.offer_actuals),
                    "total_planned": s.total_planned,
                    "total_actual": s.total_actual,
                    "is_peak": s.is_peak,
                    "capacity_pct": s.capacity_pct,
                    "state": state,
                    "status": status,
                })

            remaining = max(0, self.total_leads - completed)
            avg_rate = None
            est_finish = None
            elapsed_h = max(1e-6, (now - self.start_time).total_seconds() / 3600.0)
            if completed > 0 and now > self.start_time:
                avg_rate = completed / elapsed_h
                if avg_rate > 0:
                    est_finish = (now + timedelta(hours=remaining / avg_rate)).strftime("%-I:%M %p")
            return {
                "rows": rows,
                "current_hour": (cur_idx + 1) if cur_idx is not None else None,
                "total_hours": len(self.slots),
                "completed": completed,
                "planned_total": planned_total,
                "remaining": remaining,
                "avg_actual_rate": round(avg_rate, 1) if avg_rate else None,
                "est_finish": est_finish,
                "realistic_max": self.realistic_max,
                "drift_warning": self.drift_warning,
                "observed_time_per_lead": round(self.observed_time_per_lead, 2) if self.observed_time_per_lead else None,
            }
