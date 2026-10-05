"""Model-2 fixed expenses: actual wallet debits and unspent liabilities.

This subclass is opt-in. CashAccount v1 and its rules remain unchanged. A
reviewed schedule is a model declaration, not certification of a user's bill.
"""
import copy
from decimal import Decimal, ROUND_DOWN

from .cash_account import CashAccount, CashContractError, CURRENCIES, _day, _evidence, _keys, number, serial, stamp
from .evidence import canonical_hash

MODEL_VERSION = "historical-account-model/2"
REVIEW_VERSION = "fixed-expense-review/1"
EXPENSE_VERSION = "fixed-expense/1"


class ExpenseWallet(CashAccount):
    def __init__(self, rules, *, expense_review, fixed_expenses, model_version=MODEL_VERSION):
        if model_version != MODEL_VERSION:
            raise CashContractError("fixed expense wallet requires explicit model 2")
        self.booked_expenses = {}
        super().__init__(rules)
        self.expense_review = copy.deepcopy(expense_review)
        self.fixed_expenses = copy.deepcopy(fixed_expenses)
        self._validate_expenses()
        self.expenses_by_id = {row["id"]: row for row in self.fixed_expenses}

    def _validate_expenses(self):
        review = self.expense_review
        _keys(review, {"schema_version", "reviewer", "scope", "reviewed_at", "coverage_start_date",
                       "coverage_end_date", "explicit_zero", "schedule_sha256", "evidence"}, "expense review")
        if review["schema_version"] != REVIEW_VERSION:
            raise CashContractError("unsupported fixed expense review")
        if any(not isinstance(review[k], str) or not review[k].strip() for k in ("reviewer", "scope")):
            raise CashContractError("explicit expense reviewer and scope required")
        start, end = _day(review["coverage_start_date"]), _day(review["coverage_end_date"])
        if not start <= self.start_at.astimezone(self.timezone).date() <= end:
            raise CashContractError("expense review must cover the funded start")
        freeze = stamp(self.rules["knowledge_clock"]["freeze_at"])
        reviewed = stamp(review["reviewed_at"])
        if reviewed > freeze:
            raise CashContractError("expense review postdates freeze")
        _evidence(review["evidence"], min(reviewed, freeze), self.rules["classification"])
        if not isinstance(self.fixed_expenses, list):
            raise CashContractError("explicit fixed expense array required")
        if type(review["explicit_zero"]) is not bool or review["explicit_zero"] != (not self.fixed_expenses):
            raise CashContractError("empty schedule requires an explicit zero review")
        if review["schedule_sha256"] != canonical_hash(self.fixed_expenses):
            raise CashContractError("expense review does not bind exact schedule")
        ids, ordering = set(), []
        for row in self.fixed_expenses:
            _keys(row, {"schema_version", "id", "currency", "amount", "book_at", "category", "evidence"}, "fixed expense")
            if (row["schema_version"] != EXPENSE_VERSION or not isinstance(row["id"], str)
                    or not row["id"].strip() or row["id"] in ids or row["currency"] not in CURRENCIES
                    or row["category"] != "fixed" or not isinstance(row["amount"], str)):
                raise CashContractError("unique fixed expense identity, currency and exact amount required")
            ids.add(row["id"])
            amount = number(row["amount"], "fixed expense amount")
            quantum = number(self.rules["money_rounding"][row["currency"]]["quantum"])
            if amount < 0 or amount % quantum != 0:
                raise CashContractError("nonnegative fixed expense amount on wallet money grid required")
            at = stamp(row["book_at"])
            local_day = at.astimezone(self.timezone).date()
            if (at < self.start_at or not start <= local_day <= end
                    or at != stamp(self.cutoff_at(local_day.isoformat()))):
                raise CashContractError("expense booking must use a covered calendar cutoff")
            _evidence(row["evidence"], self._knowledge_at(at), self.rules["classification"])
            if stamp(row["evidence"]["known_at"]) > reviewed:
                raise CashContractError("expense review predates receipt of its schedule")
            ordering.append((at, row["id"]))
        if ordering != sorted(ordering):
            raise CashContractError("expense rows must ascend by booking time and id")

    @property
    def owed(self):
        totals = {currency: Decimal(0) for currency in CURRENCIES}
        for expense_id, state in self.booked_expenses.items():
            totals[self.expenses_by_id[expense_id]["currency"]] += state["remaining_owed"]
        return totals

    def available(self, currency):
        return max(Decimal(0), self.settled[currency] + min(Decimal(0), self.unpaid_interest[currency]) - self.owed[currency])

    def _covered_cutoff(self, at):
        day = at.astimezone(self.timezone).date()
        if (not _day(self.expense_review["coverage_start_date"]) <= day <= _day(self.expense_review["coverage_end_date"])
                or at != stamp(self.cutoff_at(day.isoformat()))):
            raise CashContractError("expense operation requires a reviewed calendar cutoff")

    def book_expenses(self, *, as_of):
        """Book due amounts once, then retry partial payments from settled cash.

        Call before interest accrual and optionally again after crediting. The
        returned USD payment events each require exactly one native cash debit.
        """
        at = self._time(as_of)
        self._covered_cutoff(at)
        due = [row for row in self.fixed_expenses if row["id"] not in self.booked_expenses and stamp(row["book_at"]) <= at]
        if any(stamp(row["book_at"]) != at for row in due):
            raise CashContractError("missed expense booking cutoff; historical backfill refused")
        events, usd_payments = [], []
        for row in due:
            event = self._emit("FIXED_EXPENSE_BOOKED", at, expense_id=row["id"], currency=row["currency"],
                               amount=number(row["amount"]), book_at=row["book_at"], category="fixed",
                               evidence=copy.deepcopy(row["evidence"]), schedule_sha256=self.expense_review["schedule_sha256"])
            self.booked_expenses[row["id"]] = {"book_sequence": event["sequence"], "remaining_owed": number(row["amount"])}
            events.append(event)
        for row in self.fixed_expenses:
            state = self.booked_expenses.get(row["id"])
            if state is None or state["remaining_owed"] == 0:
                continue
            currency = row["currency"]
            gross_available = max(Decimal(0), self.settled[currency] + min(Decimal(0), self.unpaid_interest[currency]))
            quantum = number(self.rules["money_rounding"][currency]["quantum"])
            amount = (min(gross_available, state["remaining_owed"]) / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum
            if amount <= 0:
                continue
            self.settled[currency] -= amount
            state["remaining_owed"] -= amount
            event = self._emit("FIXED_EXPENSE_PAYMENT", at, expense_id=row["id"], currency=currency,
                               amount=amount, remaining_owed=state["remaining_owed"], book_sequence=state["book_sequence"])
            events.append(event)
            if currency == "USD":
                usd_payments.append(copy.deepcopy(event))
        self.last_at = at
        return {"events": events, "usd_paid_total": str(sum((number(e["amount"]) for e in usd_payments), Decimal(0))),
                "usd_payment_events": usd_payments, "snapshot": self.balances()}

    def balances(self):
        result = super().balances()
        owed = self.owed
        for currency in sorted(CURRENCIES):
            result[currency].update(available=str(self.available(currency)), fixed_expense_owed=str(owed[currency]))
        return result

    def accrue_day(self, day, *, as_of):
        at = self._time(as_of)
        self._covered_cutoff(at)
        due = {row["id"] for row in self.fixed_expenses if stamp(row["book_at"]) <= at}
        if not due <= set(self.booked_expenses):
            raise CashContractError("book fixed expenses before interest accrual")
        owed = self.owed
        for currency in CURRENCIES:
            gross = max(Decimal(0), self.settled[currency] + min(Decimal(0), self.unpaid_interest[currency]))
            q = number(self.rules["money_rounding"][currency]["quantum"])
            payable = (min(gross, owed[currency]) / q).to_integral_value(rounding=ROUND_DOWN) * q
            if payable > 0:
                raise CashContractError("pay available fixed expenses before interest accrual")
        return super().accrue_day(day, as_of=as_of)

    def exchange(self, direction, amount, *, execution_quote, as_of, event_id, extra_spread_rate="0"):
        at = self._time(as_of)
        if direction not in {"CNY_TO_USD", "USD_TO_CNY"}:
            raise CashContractError("explicit FX direction required")
        source = "CNY" if direction == "CNY_TO_USD" else "USD"
        _, _, buy_fee, sell_fee = self._fx(execution_quote, at, execution=True, extra_spread_rate=extra_spread_rate)
        debit = number(amount) + (buy_fee if source == "CNY" else sell_fee)
        if debit > self.available(source):
            raise CashContractError("fixed expense liabilities reserve this wallet; no implicit FX funding")
        return super().exchange(direction, amount, execution_quote=execution_quote, as_of=as_of,
                                event_id=event_id, extra_spread_rate=extra_spread_rate)

    def snapshot(self, *, as_of, fx_mark, usd_assets, exit_quote=None, extra_spread_rate="0"):
        result = super().snapshot(as_of=as_of, fx_mark=fx_mark, usd_assets=usd_assets,
                                  exit_quote=exit_quote, extra_spread_rate=extra_spread_rate)
        at, owed = stamp(as_of), self.owed
        mid = number(result["fx_mark_cny_per_usd"])
        result["usd_total_equity"] = str(number(result["usd_total_equity"]) - owed["USD"])
        result["marked_equity_cny"] = str(number(result["marked_equity_cny"]) - owed["CNY"] - owed["USD"] * mid)
        result["fixed_expense_liabilities"] = serial(owed)
        result["expense_schedule_sha256"] = self.expense_review["schedule_sha256"]
        if exit_quote is not None:
            _, bid, _, fee = self._fx(exit_quote, at, execution=True, extra_spread_rate=extra_spread_rate)
            usd, cny = self.available("USD"), self.available("CNY")
            result["settled_cash_exit_cny"] = str(cny if usd == 0 else cny + self._money((usd-fee)*bid, "CNY")) if usd == 0 or usd >= fee else None
            if owed["CNY"] or owed["USD"]:
                result["exit_equity_cny"] = None
                result["exit_limit"] = "unpaid fixed expenses prevent a fully discharged exit; settled cash is reserved"
            elif result["exit_equity_cny"] is not None:
                result["exit_equity_cny"] = result["settled_cash_exit_cny"]
        return result

    def restore_expenses_from_events(self):
        """Rebuild expense state after restoring the existing wallet journal."""
        if ([event.get("sequence") for event in self.events] != list(range(1, len(self.events)+1))
                or any(type(event.get("sequence")) is not int for event in self.events)):
            raise CashContractError("expense checkpoint requires contiguous integer journal sequences")
        states = {}
        for event in self.events:
            if event["type"] == "FIXED_EXPENSE_BOOKED":
                expense_id = event["expense_id"]
                row = self.expenses_by_id.get(expense_id)
                if (row is None or expense_id in states or event["currency"] != row["currency"]
                        or number(event["amount"]) != number(row["amount"])
                        or stamp(event["at"]) != stamp(row["book_at"])
                        or event["book_at"] != row["book_at"] or event["category"] != "fixed"
                        or event["evidence"] != row["evidence"]
                        or event["knowledge_clock"] != self.rules["knowledge_clock"]
                        or stamp(event["at"]) > self.last_at
                        or event["schedule_sha256"] != self.expense_review["schedule_sha256"]):
                    raise CashContractError("invalid or duplicate expense checkpoint booking")
                states[expense_id] = {"book_sequence": event["sequence"], "remaining_owed": number(row["amount"])}
            elif event["type"] == "FIXED_EXPENSE_PAYMENT":
                expense_id = event["expense_id"]
                state = states.get(expense_id)
                amount = number(event["amount"])
                if (state is None or event["book_sequence"] != state["book_sequence"]
                        or type(event["book_sequence"]) is not int
                        or event["knowledge_clock"] != self.rules["knowledge_clock"]
                        or event["currency"] != self.expenses_by_id[expense_id]["currency"]
                        or not 0 < amount <= state["remaining_owed"] or stamp(event["at"]) > self.last_at):
                    raise CashContractError("invalid expense checkpoint payment")
                self._covered_cutoff(stamp(event["at"]))
                state["remaining_owed"] -= amount
                if number(event["remaining_owed"]) != state["remaining_owed"]:
                    raise CashContractError("expense checkpoint remaining liability differs")
        expected = {row["id"] for row in self.fixed_expenses if stamp(row["book_at"]) <= self.last_at}
        if set(states) != expected:
            raise CashContractError("expense checkpoint omits a due schedule row")
        self.booked_expenses = states
