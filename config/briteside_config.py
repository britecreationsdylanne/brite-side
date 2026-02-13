"""
The BriteSide - Internal Monthly Newsletter Configuration
Employee data, system prompts, and template settings.
"""

# ──────────────────────────────────────────
# Employee List
# Replace placeholder data with real employee info
# ──────────────────────────────────────────

EMPLOYEES = [
    {"name": "Dylanne Crugnale", "email": "dylanne.crugnale@brite.co", "birthday_month": 1, "birthday_day": 15, "department": "Marketing", "title": "Marketing Manager"},
    {"name": "Selena Fragassi", "email": "selena.fragassi@brite.co", "birthday_month": 2, "birthday_day": 10, "department": "Content", "title": "Content Director"},
    {"name": "John Ortbal", "email": "john.ortbal@brite.co", "birthday_month": 3, "birthday_day": 5, "department": "Sales", "title": "Sales Director"},
    {"name": "Stef Lynn", "email": "stef.lynn@brite.co", "birthday_month": 4, "birthday_day": 20, "department": "Operations", "title": "Operations Manager"},
    {"name": "Rachel Akmakjian", "email": "rachel.akmakjian@brite.co", "birthday_month": 5, "birthday_day": 8, "department": "Content", "title": "Content Writer"},
    {"name": "Sam McGregor", "email": "sam.mcregor@brite.co", "birthday_month": 6, "birthday_day": 12, "department": "Customer Success", "title": "CS Team Lead"},
    {"name": "Alex Johnson", "email": "alex.johnson@brite.co", "birthday_month": 7, "birthday_day": 3, "department": "Engineering", "title": "Software Engineer"},
    {"name": "Jordan Lee", "email": "jordan.lee@brite.co", "birthday_month": 8, "birthday_day": 28, "department": "Product", "title": "Product Manager"},
    {"name": "Morgan Smith", "email": "morgan.smith@brite.co", "birthday_month": 9, "birthday_day": 17, "department": "Design", "title": "UI/UX Designer"},
    {"name": "Casey Rivera", "email": "casey.rivera@brite.co", "birthday_month": 10, "birthday_day": 22, "department": "Sales", "title": "Account Executive"},
    {"name": "Taylor Chen", "email": "taylor.chen@brite.co", "birthday_month": 11, "birthday_day": 6, "department": "Engineering", "title": "Backend Developer"},
    {"name": "Riley Kim", "email": "riley.kim@brite.co", "birthday_month": 12, "birthday_day": 14, "department": "Customer Success", "title": "CS Specialist"},
]

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December"
}


# ──────────────────────────────────────────
# AI System Prompt
# ──────────────────────────────────────────

BRITESIDE_SYSTEM_PROMPT = (
    "You are a fun, punny internal newsletter writer for BriteCo, a jewelry insurance company. "
    "This newsletter is called 'The BriteSide' — it's the monthly internal employee newsletter.\n\n"
    "VOICE & TONE:\n"
    "- Fun, warm, celebratory, and punny\n"
    "- Think: the cool coworker who organizes birthday celebrations\n"
    "- Jewelry puns, wedding puns, and watch puns are ALWAYS welcome\n"
    "- Keep it upbeat and positive — this is internal morale-building\n"
    "- Light humor, emoji-friendly, casual but professional\n\n"
    "CONTEXT:\n"
    "- BriteCo provides jewelry insurance, watch insurance, and event insurance\n"
    "- The audience is internal employees who know the company well\n"
    "- No need to explain what BriteCo does — they work here!\n\n"
    "AVOID:\n"
    "- Corporate-speak or HR-sounding language\n"
    "- Overly formal tone\n"
    "- Anything that sounds like a press release\n"
    "- Generic 'we are a family' platitudes without personality"
)


# ──────────────────────────────────────────
# AI Prompt Templates
# ──────────────────────────────────────────

AI_PROMPTS = {
    "generate_joke": (
        "Write 3 short, fun jokes or puns related to {theme}. "
        "These are for the opening of an internal company newsletter at a jewelry insurance company.\n\n"
        "Requirements:\n"
        "- Each joke should be 1-2 sentences max\n"
        "- Puns about jewelry, weddings, watches, or insurance are great\n"
        "- Keep it office-appropriate and upbeat\n"
        "- Number them 1, 2, 3 so the user can pick their favorite\n"
        "- Seasonal tie-ins for {month} are welcome\n\n"
        "Return ONLY the 3 numbered jokes, nothing else."
    ),
    "generate_spotlight": (
        "Write a fun, warm employee spotlight blurb for an internal company newsletter.\n\n"
        "Employee: {name}\n"
        "Title: {title}\n"
        "Department: {department}\n"
        "Fun Facts: {fun_facts}\n\n"
        "Requirements:\n"
        "- 3-4 sentences, max 80 words\n"
        "- Celebratory and warm tone\n"
        "- Weave in the fun facts naturally\n"
        "- Include a jewelry/insurance pun if it fits naturally\n"
        "- Do NOT include any heading or label — return ONLY the blurb text"
    ),
    "generate_birthday_message": (
        "Write a short, fun birthday shoutout for an internal company newsletter. "
        "The birthday person is {name} from {department}.\n\n"
        "Month: {month}\n\n"
        "Requirements:\n"
        "- 1 sentence, max 20 words\n"
        "- Fun, warm, punny if possible\n"
        "- Jewelry/sparkle themed is great\n"
        "- Return ONLY the shoutout text, no names or labels"
    ),
}


# ──────────────────────────────────────────
# Email Template Configuration
# ──────────────────────────────────────────

EMAIL_TEMPLATE_CONFIG = {
    "template_file": "templates/briteside-email.html",
    "container_width": 640,
    "colors": {
        "header_bg": "#272d3f",
        "accent_teal": "#018181",
        "accent_orange": "#FE8916",
        "birthday_bg": "#FFF8F0",
        "spotlight_bg": "#F0FAFA",
        "text_dark": "#272d3f",
        "text_light": "#ffffff",
        "text_muted": "#6b7280",
        "footer_bg": "#272d3f",
    },
}

SENDGRID_CONFIG = {
    "from_email": "newsletter@brite.co",
    "from_name": "The BriteSide",
}

# ──────────────────────────────────────────
# Google Cloud Storage Configuration
# ──────────────────────────────────────────

GCS_CONFIG = {
    "drafts_bucket": "brite-side-drafts",
    "drafts_prefix": "drafts/",
    "published_prefix": "published/",
}
