"""Agent schema — 19 FunctionDeclarations + SYSTEM_INSTRUCTION.

Port of:
  src/features/agent/lib/schema.ts
  src/features/agent/lib/domains/students.ts
  src/features/agent/lib/domains/templates.ts
  src/features/agent/lib/domains/timetable.ts
"""

from google.genai import types

from app.shared.utils import DAYS
from app.features.templates.service import TEMPLATE_META

# ---------------------------------------------------------------------------
# Student declarations (11 tools)
# ---------------------------------------------------------------------------

STUDENT_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_students",
        description="Search for students by name (partial match). Returns id, name, status, class_schedule. Use before update/delete/get to obtain the student ID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING, description="Partial or full student name"),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_student",
        description="Fetch full details of a single student by UUID — all fields including fee, notes, homework, contact info, Google links, and portal access emails. Call search_students first to get the UUID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "id": types.Schema(type=types.Type.STRING, description="Student UUID obtained from search_students"),
            },
            required=["id"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_students",
        description="List students with an optional status filter. Use when the user asks to see all students or students with a specific status. For day-based schedule queries (\"who do I have on Monday?\"), use get_schedule instead.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "status": types.Schema(
                    type=types.Type.STRING,
                    enum=["Active", "On Hold", "Completed"],
                    description="Filter by student status (optional)",
                ),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="create_student",
        description="Create a new student record in the database.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "name": types.Schema(type=types.Type.STRING),
                "mode": types.Schema(
                    type=types.Type.STRING,
                    enum=["My Python Syllabus", "Other Syllabus"],
                ),
                "fee_per_hour": types.Schema(type=types.Type.NUMBER, description="Hourly fee in RM"),
                "payment_method": types.Schema(type=types.Type.STRING, enum=["Monthly", "Weekly"]),
                "status": types.Schema(
                    type=types.Type.STRING,
                    enum=["Active", "On Hold", "Completed"],
                ),
                "class_schedule": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "day": types.Schema(type=types.Type.STRING, enum=DAYS),
                            "start": types.Schema(type=types.Type.STRING, description='24-hour HH:MM format, e.g. "15:00"'),
                            "end": types.Schema(type=types.Type.STRING, description='24-hour HH:MM format, e.g. "17:00"'),
                        },
                        required=["day", "start", "end"],
                    ),
                ),
                "contact_person": types.Schema(type=types.Type.STRING),
                "contact_phone": types.Schema(type=types.Type.STRING),
                "student_phone": types.Schema(type=types.Type.STRING),
                "today_homework": types.Schema(type=types.Type.STRING),
                "notes": types.Schema(type=types.Type.STRING),
                "latest_payment": types.Schema(type=types.Type.STRING),
                "access_emails": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING),
                    description="Portal login emails for the student/parent",
                ),
                "google_meet_link": types.Schema(type=types.Type.STRING, description="Google Meet URL"),
                "google_drive_link": types.Schema(type=types.Type.STRING, description="Google Drive folder URL"),
            },
            required=["name", "mode", "fee_per_hour"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_student",
        description="Update one or more fields on an existing student. You MUST call search_students first to obtain the student UUID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "id": types.Schema(
                    type=types.Type.STRING,
                    description="Student UUID obtained from search_students",
                ),
                "fields": types.Schema(
                    type=types.Type.OBJECT,
                    description="Object of fields to update. Allowed keys: name, mode, fee_per_hour, payment_method, status, class_schedule, contact_person, contact_phone, student_phone, today_homework, notes, latest_payment, google_meet_link, google_drive_link, access_emails. When updating access_emails, provide the full desired list — adds and removes are handled automatically by diffing against the current list.",
                ),
            },
            required=["id", "fields"],
        ),
    ),
    types.FunctionDeclaration(
        name="delete_student",
        description='Permanently delete a student record. Only call this AFTER the user has typed "yes" to confirm deletion in this conversation.',
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "id": types.Schema(
                    type=types.Type.STRING,
                    description="Student UUID obtained from search_students",
                ),
            },
            required=["id"],
        ),
    ),
    types.FunctionDeclaration(
        name="setup_student_google",
        description="Set up Google Calendar weekly events and Drive folder for a student. Creates Calendar events (generating a Meet link) then creates the Drive folder. Skips whichever is already done. You MUST call search_students first to get the student UUID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "student_id": types.Schema(
                    type=types.Type.STRING,
                    description="Student UUID obtained from search_students",
                ),
            },
            required=["student_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="sync_all_students",
        description="Sync all active students' Google Calendar events and Drive Meet docs to match the database schedule. Affects every active student — always confirm with the user before calling.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="manage_portal_access",
        description="Add or remove an email address from a student's portal access list. Use search_students first to get the student UUID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "student_id": types.Schema(type=types.Type.STRING, description="Student UUID obtained from search_students"),
                "action": types.Schema(type=types.Type.STRING, enum=["add", "remove"], description="Whether to add or remove the email"),
                "email": types.Schema(type=types.Type.STRING, description="Email address to add or remove"),
            },
            required=["student_id", "action", "email"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_schedule",
        description="Get the list of students who have class on a given day of the week. Returns student names and their slot times for that day.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "day": types.Schema(
                    type=types.Type.STRING,
                    enum=DAYS,
                    description="Day of the week",
                ),
            },
            required=["day"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_fee_summary",
        description="Calculate total monthly tuition fee revenue across all active students. Uses exact session counts for the given month.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "month": types.Schema(
                    type=types.Type.NUMBER,
                    description="Month number 1–12 (optional, defaults to current month in MYT)",
                ),
                "year": types.Schema(
                    type=types.Type.NUMBER,
                    description="Year e.g. 2026 (optional, defaults to current year in MYT)",
                ),
            },
        ),
    ),
]

# ---------------------------------------------------------------------------
# Template declarations (3 tools)
# ---------------------------------------------------------------------------

TEMPLATE_DECLARATIONS = [
    types.FunctionDeclaration(
        name="list_templates",
        description="List all message templates with their id, title, and description — no content. Use this only to discover which template ID to use, then call get_template with that ID to fetch the actual content.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="get_template",
        description="Fetch a single message template by its id. Call list_templates first if you are unsure which id the user means.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "id": types.Schema(
                    type=types.Type.STRING,
                    enum=list(TEMPLATE_META.keys()),
                    description="Template id",
                ),
            },
            required=["id"],
        ),
    ),
    types.FunctionDeclaration(
        name="generate_payment_message",
        description="Generate a ready-to-send payment reminder message for a student. Automatically calculates session dates and total fee from the student's schedule and fee rate. Defaults to next calendar month if month/year are not specified.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "student_id": types.Schema(type=types.Type.STRING, description="Student UUID"),
                "month": types.Schema(
                    type=types.Type.NUMBER,
                    description="Month 1–12 (optional, defaults to next month in MYT)",
                ),
                "year": types.Schema(
                    type=types.Type.NUMBER,
                    description="Year e.g. 2026 (optional, defaults to next month's year in MYT)",
                ),
                "template_type": types.Schema(
                    type=types.Type.NUMBER,
                    description="1 = standard reminder, 2 = with carryover sessions deducted from total (optional, defaults to 1)",
                ),
                "carryover": types.Schema(
                    type=types.Type.NUMBER,
                    description="Number of sessions from the previous month to carry over and deduct (only used when template_type is 2)",
                ),
            },
            required=["student_id"],
        ),
    ),
]

# ---------------------------------------------------------------------------
# Timetable declarations (5 tools)
# ---------------------------------------------------------------------------

TIMETABLE_DECLARATIONS = [
    types.FunctionDeclaration(
        name="get_timetable_settings",
        description="Read the current timetable scheduling rules and buffer minutes from the database. Call this before update_timetable_rules or update_buffer_mins to show the user the current values.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="update_timetable_rules",
        description="Save new scheduling rules text to the database. These rules guide the AI slot generator (preferred/normal/unavailable classification). Always show the user the new rules before calling.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "rules": types.Schema(
                    type=types.Type.STRING,
                    description="Full scheduling rules text to save",
                ),
            },
            required=["rules"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_buffer_mins",
        description="Save a new buffer duration (in minutes) to the database. Buffer zones block slots immediately before/after booked classes. Valid range: 0–60.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "buffer_mins": types.Schema(
                    type=types.Type.NUMBER,
                    description="Buffer duration in minutes (0–60)",
                ),
            },
            required=["buffer_mins"],
        ),
    ),
    types.FunctionDeclaration(
        name="generate_slot_availability",
        description='Run the AI slot-availability generator. Reads current rules, buffer, and all active students\' schedules from the database, then classifies every free 30-minute slot as preferred, normal, or unavailable. Optionally accepts a description of a new student\'s availability to bias the classification. After the tool completes, a "Download PNG" button appears automatically in the chat.',
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "student_availability": types.Schema(
                    type=types.Type.STRING,
                    description='Free-text description of when a prospective student can attend (optional). Example: "free Tuesday and Thursday after 4pm".',
                ),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="download_timetable_image",
        description='Download the weekly schedule as a PNG image showing all active students\' class blocks. After the tool completes, a "Download PNG" button appears automatically in the chat.',
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
]

# ---------------------------------------------------------------------------
# Combined tool list (all 19)
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    types.Tool(function_declarations=STUDENT_DECLARATIONS + TEMPLATE_DECLARATIONS + TIMETABLE_DECLARATIONS)
]

# ---------------------------------------------------------------------------
# System instruction (verbatim from TypeScript)
# ---------------------------------------------------------------------------

_STUDENT_RULES = """1. Before calling get_student, update_student, delete_student, setup_student_google, or manage_portal_access, you need the student's UUID. If it already appears earlier in this conversation, reuse it directly — do not call search_students again. Only call search_students if the UUID is not already known.
2. Never call delete_student without first asking: "Are you sure you want to permanently delete [name]? Type yes to confirm." You must see "yes" in the conversation before proceeding.
3. If a create_student command is missing required fields (mode, fee_per_hour), ask for them before calling the tool.
4. If search_students returns multiple matches, list them and ask which student the user means.
5. If search_students returns no results when the user wanted to update/delete, say so and offer to create instead.
6. After successfully creating or updating one or more students, append one token per student at the end of your reply in this exact format: [student_id:NAME:UUID] where NAME is the student's name and UUID is their UUID. Example for two students: [student_id:Lynn:uuid-1] [student_id:Ang:uuid-2]. The UI will render a "View NAME →" link for each token.
7. Keep replies concise and friendly. Always use clean markdown formatting in your replies:
    - Use tables whenever displaying multiple records or multiple fields side by side.
    - Use bold labels for single-record detail views (e.g. **Name:** Ang).
    - Never show raw UUIDs in the reply body — the UI renders a "View student" link separately.
    - Skip fields that are null, empty, or "-" — do not print them at all.
    - Render Google Meet and Drive URLs as markdown links: [Meet link](url), [Drive folder](url).
    - For multi-line fields like notes or homework, use a blockquote (> text).
    - When displaying list_students results, use a table with columns: Name | Mode | Fee/hr | Schedule. Compress schedule into one cell e.g. "Mon 18:45–19:45, Wed 11:00–12:00". Do NOT call get_student for each result.
    - Use list_students (not search_students) whenever the user asks to see all students, active students, or any roster-style query — even if they don't say the word "list". For day-based queries ("who do I have on Monday?"), use get_schedule instead.
    - When displaying a single student's full details, group fields: basic info → contact → schedule → Google → other.
8. Before calling sync_all_students, ask the user: "This will sync Google Calendar and Drive for all active students. Confirm?" and wait for explicit confirmation.
9. When asking the user to confirm deletion (before calling delete_student), state explicitly that their Google Calendar events and Drive folder will also be permanently removed.
10. After a successful setup_student_google, also include the student token in your reply using the same format as Rule 6: [student_id:NAME:UUID]
11. If a tool result contains suggestGoogleSetup: true, ask the user: "Would you like me to also set up Google Calendar and Drive for [student name]?" and wait for their reply. Only call setup_student_google if they say yes.
12. Use get_schedule when the user asks who they have class with on a specific day. The current date is injected at the top of this prompt — use it to resolve "today", "tomorrow", and relative day references to the correct Monday–Sunday day name before calling. Format results as a table: Name | Time (12-hour format, e.g. 3:00 PM – 5:00 PM). If students is empty, say "No classes on [day]."
13. Use get_fee_summary when the user asks about monthly revenue, total fees, income, or earnings — whether for all students or a specific student. If no month or year is specified, omit them from the tool call (the tool defaults to the current month). The tool returns per-student fees; if the user asked about a specific student, find that student in the returned list and report only their fee. Format all-student results as a table: Name | Fee (RM) with a bold **Total** row. For a single-student query, just state their fee directly."""

_TEMPLATE_RULES = """15. For template requests: if the user names a specific template (e.g. "payment", "first approach", "review"), call get_template directly with the matching id. If it is unclear which template they mean, call list_templates first. When displaying a template, format your reply as: one line with the title (e.g. "**First Approach**"), then a blank line, then the full content inside a fenced code block (triple backticks, no language tag) so it is easy to copy. Never put the title and "Content:" label on the same line.
16. Use generate_payment_message when the user asks to generate a payment message or reminder for a student. If no month or year is specified, omit them (the tool defaults to next month). Ask whether to use carryover (template_type 2) only if the user mentions it — otherwise default to template_type 1. Display the result with a one-line header (e.g. "**Payment reminder — June 2026**") then the message in a fenced code block for easy copying."""

_TIMETABLE_RULES = """17. Timetable settings: use get_timetable_settings to read current rules and buffer before updating. When the user asks to update rules, show them the proposed new rules and confirm before calling update_timetable_rules. For update_buffer_mins, validate the value is 0–60 before calling.
18. After calling generate_slot_availability or download_timetable_image, tell the user a download button has appeared in the chat. Do NOT describe the slot counts or classification details unless the user asks — keep the reply brief (one sentence)."""

SYSTEM_INSTRUCTION = f"""You are a helpful assistant for a private tuition admin system. You help the tutor manage student records using the provided tools.

RULES — follow these exactly:
{_STUDENT_RULES}
14. When the user's request involves multiple independent operations, call all the relevant tools in a single response round rather than one at a time. For example: if asked to search for two students, call search_students for both in the same round; if asked to update two students whose IDs are already known, call update_student for both in the same round. Only serialise tool calls when one call's output is required as input for the next call.
{_TEMPLATE_RULES}
{_TIMETABLE_RULES}"""
