"""
The BriteSide - Internal Monthly Newsletter Configuration
Employee data, system prompts, and template settings.
"""

# ──────────────────────────────────────────
# Employee List
# Replace placeholder data with real employee info
# ──────────────────────────────────────────

EMPLOYEES = [
    {"name": "Jack Courtad", "email": "jack.courtad@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Dove", "email": "dove@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Ben Glispie", "email": "ben.glispie@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Ben Mautner", "email": "ben.mautner@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Brenno Cardoso", "email": "brenno.cardoso@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Dustin Lemick", "email": "dustin.lemick@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Hannah Greene-Gretzinger", "email": "hannah.greene-gretzinger@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Kaitlyn Rigdon", "email": "kaitlyn.rigdon@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Kevin Walters", "email": "kevin.walters@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Stephanie Lynn", "email": "stephanie.lynn@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Paxton Washington", "email": "paxton.washington@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Dana Koutnik", "email": "dana.koutnik@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Teagyn Lindley", "email": "teagyn.lindley@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Brian Kelly", "email": "brian.kelly@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Cameron Chapman", "email": "cameron.chapman@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Dustin Sitar", "email": "dustin.sitar@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Dylanne Crugnale", "email": "dylanne.crugnale@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Edgar Diengdoh", "email": "edgar.diengdoh@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Esme Galvan", "email": "esme.galvan@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "John Ortbal", "email": "john.ortbal@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Lauren Appenfeller", "email": "lauren.appenfeller@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Rachel Akmakjian", "email": "rachel.akmakjian@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Sam MacGregor", "email": "sam.macgregor@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Selena Fragassi", "email": "selena.fragassi@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Stephanie Block", "email": "stephanie.block@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Madisyn Kafer", "email": "madisyn.kafer@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Saba Patel", "email": "saba.patel@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Sheena Sims", "email": "sheena.sims@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Alexia Trejo", "email": "alexia.trejo@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Christine Belling", "email": "christine.belling@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Conor Redmond", "email": "conor.redmond@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Ellie Asiuras", "email": "ellie.asiuras@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
    {"name": "Ryan McQuilkin", "email": "ryan.mcquilkin@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": ""},
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
    "ABOUT BRITECO:\n"
    "- BriteCo is a leading jewelry and watch insurance provider, founded by Dustin Lemick\n"
    "- Products: jewelry insurance, watch insurance, and event insurance (wedding/engagement)\n"
    "- BriteCo offers instant online appraisals and easy claims — modernizing jewelry insurance\n"
    "- The company works with jewelers, retailers, and direct consumers nationwide\n"
    "- Core values: transparency, simplicity, and protecting what matters most to customers\n"
    "- BriteCo partners with thousands of jewelers across the US and Canada\n"
    "- The team is small, tight-knit, and remote-friendly\n"
    "- Key differentiators: replacement value coverage (not depreciated), easy digital appraisals, "
    "quick claims process, and coverage that travels worldwide\n\n"
    "VOICE & TONE:\n"
    "- Fun, warm, celebratory, and punny\n"
    "- Think: the cool coworker who organizes birthday celebrations\n"
    "- Jewelry puns, wedding puns, and watch puns are ALWAYS welcome\n"
    "- Keep it upbeat and positive — this is internal morale-building\n"
    "- Light humor, emoji-friendly, casual but professional\n\n"
    "CONTEXT:\n"
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
