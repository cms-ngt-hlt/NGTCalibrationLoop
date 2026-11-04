"""The one-pod deployment's banner on the UI's Home page, the only thing there that leads to its login:
anyone can read the UI without one, and nothing in Airflow's own menus offers it then."""

from airflow.api_fastapi.common.types import UIAlert

DASHBOARD_UIALERTS = [UIAlert(text="Anyone can look around here. To change anything, [log in](/auth/login/).", category="info")]
