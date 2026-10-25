"""Desk rules (04_BACKEND §4): the mechanics of the desk's conventions, parametrised by the organisation's
rules as data (the private adapter's ``reference()``, ADR-008). No firm values live here: every open owner
decision is a policy field with a neutral default or none (``funding.OPEN_DECISIONS``).

- ``funding`` — the funding engine (FND-5): daily charge and accrual of positions, HTM lots, cash and REPO.
- ``reference`` — the adapter's ``reference()`` funding rules and entities (private/1.2) as the engine's inputs.
- ``ocp`` — the OCP from positions by component, its reconciliation with the reported OCP, the OCP transfers (B2,
  TDM#59; ``ocp.OPEN_DECISIONS``).
- ``rate_index`` — index fixings and FX rates in force on a day, behind injectable providers.
"""
