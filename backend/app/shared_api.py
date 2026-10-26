"""The endpoints shared with other apps and AI agents through WorkflowHub (node.yaml `api:`).

WorkflowHub forwards a call as the person asking, with a signed `X-Wfh-User` header (HS256, issuer "workflowhub",
secret WFH_APP_API_SECRET, audience WFH_APP_API_AUDIENCE). The person is matched to their clinic staff account, and
the endpoint's usual role check applies: an agent can do only what that person could do in the app.

WorkflowHub already refuses endpoints that aren't declared; the app refuses them too (defence in depth), so a
misconfigured platform still can't reach anything else with X-Wfh-User. Keep this list and node.yaml in step
(tests/test_shared_api.py checks they match).
"""

# (method, path as served, with the /api prefix) -> node.yaml endpoint key
SHARED: dict[tuple[str, str], str] = {
    ("GET", "/api/catalogue"): "list_catalogue",
    ("GET", "/api/doctor/queue"): "doctor_queue",
    ("GET", "/api/doctor/records"): "search_records",
    ("GET", "/api/visits/{visit_id}/summary"): "visit_summary",
    ("GET", "/api/ot/schedule"): "room_schedule",
    ("GET", "/api/ot/availability"): "free_slots",
    ("GET", "/api/ot/tentative"): "bookings_to_confirm",
    ("GET", "/api/ot/bookings/{booking_id}"): "get_booking",
    ("POST", "/api/ot/bookings/{booking_id}/confirm"): "confirm_booking",
    ("GET", "/api/ot/reports"): "room_report",
}
