"""
utils/sheet_handler.py
──────────────────────
Google Sheets integration via Service Account.

Responsibilities:
  • Authenticate with Google using a service-account JSON key.
  • Fetch all rows where Status == "Pending".
  • Lock a row by setting Status → "In Progress".
  • Write back results (Success / Failed / Retry) with metadata.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import gspread
import requests
from google.oauth2.service_account import Credentials
import structlog

log = structlog.get_logger(__name__)

# Google API scopes required for Sheets + Drive (read/write)
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetHandler:
    """Manages all interactions with a single Google Sheet worksheet."""

    def __init__(
        self,
        config: dict,
        sheet_url: str | None = None,
        worksheet_name: str | None = None,
    ) -> None:
        """
        Args:
            config: Parsed application config dict (from config.yaml + .env).
            sheet_url: Spreadsheet URL or ID.  Passed explicitly by the caller
                so concurrently-running engines don't race on the shared
                GOOGLE_SHEET_* env vars.  Falls back to env when omitted.
            worksheet_name: Worksheet/tab name (same fallback behaviour).
        """
        self._config = config
        self._col_map: dict[str, str] = config.get("sheet_columns", {})

        # Sheet target — explicit args win over env so each engine writes back
        # to its own worksheet even when several run at once (scheduler).
        self._sheet_url = sheet_url or os.getenv("GOOGLE_SHEET_URL", "")
        self._worksheet_name = worksheet_name or os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")

        self._worksheet = self._open_worksheet()
        self._headers: list[str] = self._worksheet.row_values(1)
        self._ensure_result_columns()
        log.info("sheet.connected", worksheet=self._worksheet_name, columns=len(self._headers))

    def _open_worksheet(self):
        """Authenticate and open the configured worksheet."""
        creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/credentials.json")
        credentials = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
        client = gspread.authorize(credentials)
        if self._sheet_url.startswith("http"):
            spreadsheet = client.open_by_url(self._sheet_url)
        else:
            # Treat as spreadsheet ID
            spreadsheet = client.open_by_key(self._sheet_url)
        return spreadsheet.worksheet(self._worksheet_name)

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_result_columns(self) -> None:
        """Add any missing result columns to the sheet header row."""
        result_fields = ["status", "notes", "proxy_used", "ip", "last_attempt",
                         "submission_id", "retry_count"]
        missing = [
            self._col_map.get(f, f)
            for f in result_fields
            if self._col_map.get(f, f) not in self._headers
        ]
        if not missing:
            return

        # Expand the grid if the sheet doesn't have enough columns.
        needed_cols = len(self._headers) + len(missing)
        if needed_cols > self._worksheet.col_count:
            self._worksheet.resize(
                rows=self._worksheet.row_count,
                cols=needed_cols,
            )

        for col_name in missing:
            next_col = len(self._headers) + 1
            self._worksheet.update_cell(1, next_col, col_name)
            self._headers.append(col_name)

        log.info("sheet.columns_added", columns=missing)

    def _col_index(self, internal_name: str) -> int:
        """Return 1-based column index for an internal field name."""
        header = self._col_map.get(internal_name, internal_name)
        try:
            return self._headers.index(header) + 1
        except ValueError:
            raise KeyError(f"Column '{header}' not found in sheet headers: {self._headers}")

    def _now_iso(self) -> str:
        """Current UTC timestamp in ISO-8601."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _reconnect(self) -> None:
        """Re-authorise and re-open the worksheet after a dropped connection."""
        self._worksheet = self._open_worksheet()

    def _retry(self, fn):
        """Run a gspread call, retrying transient connection resets / 5xx / rate
        limits.  On a connection-level drop the worksheet is reconnected so the
        next attempt uses a fresh socket.  ``fn`` must read ``self._worksheet``
        lazily (pass a no-arg lambda) so reconnects take effect."""
        last_err: Exception | None = None
        for attempt in range(4):  # 1 try + 3 retries
            try:
                return fn()
            except gspread.exceptions.APIError as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code not in (429, 500, 502, 503, 504):
                    raise
                last_err = e
            except (OSError, requests.exceptions.RequestException) as e:
                last_err = e
            log.warning("sheet.retry", attempt=attempt + 1, error=str(last_err)[:90])
            time.sleep(min(2 ** attempt, 8))
            try:
                self._reconnect()
            except Exception:
                pass
        raise last_err  # type: ignore[misc]

    # ── public API ───────────────────────────────────────────────────

    def get_pending_rows(self) -> list[dict[str, Any]]:
        """
        Return all rows where the Status column == 'Pending'.

        Each dict contains:
          • ``_row_number``  – the 1-based sheet row (for updates)
          • every column header → cell value
        """
        # numericise_ignore=['all'] keeps every cell as a string so leading
        # zeros (DL/state-id, routing, zip, SSN) are preserved — gspread would
        # otherwise convert "0123456" → 123456 and drop the leading zero.
        all_records = self._retry(
            lambda: self._worksheet.get_all_records(numericise_ignore=["all"])
        )
        status_col = self._col_map.get("status", "Status")
        pending: list[dict[str, Any]] = []

        for idx, record in enumerate(all_records, start=2):  # row 1 = header
            val = str(record.get(status_col, "")).strip().lower()
            if val in ("pending", ""):
                record["_row_number"] = idx
                pending.append(record)

        log.info("sheet.pending_rows", count=len(pending))
        return pending

    def mark_in_progress(self, row_number: int) -> None:
        """Set Status → 'In Progress' for a given row."""
        col = self._col_index("status")
        self._retry(lambda: self._worksheet.update_cell(row_number, col, "In Progress"))
        log.debug("sheet.status_update", row=row_number, status="In Progress")

    def update_row(
        self,
        row_number: int,
        *,
        status: str,
        notes: str = "",
        proxy_used: str = "",
        ip: str = "",
        submission_id: str = "",
        retry_count: int | None = None,
    ) -> None:
        """
        Write result metadata back to the sheet for one row.

        Args:
            row_number: 1-based row in the worksheet.
            status:     "Success", "Failed", or "Retry".
            notes:      Human-readable description of what happened.
            proxy_used: The proxy address used for this attempt.
            ip:         The proxy IP address (stored in 'ip' column).
            submission_id: Any ID returned by the target site.
            retry_count: Current retry counter value.
        """
        updates: list[tuple[str, str | int]] = [
            ("status", status),
            ("notes", notes),
            ("proxy_used", proxy_used),
            ("ip", ip),
            ("last_attempt", self._now_iso()),
            ("submission_id", submission_id),
        ]
        if retry_count is not None:
            updates.append(("retry_count", retry_count))

        for field, value in updates:
            try:
                col = self._col_index(field)
            except KeyError:
                # Column doesn't exist in the sheet — skip silently
                log.warning("sheet.column_missing", field=field)
                continue
            self._retry(lambda c=col, v=value: self._worksheet.update_cell(row_number, c, v))

        log.info("sheet.row_updated", row=row_number, status=status)
