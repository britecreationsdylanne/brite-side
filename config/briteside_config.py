"""
The BriteSide - Internal Monthly Newsletter Configuration
Employee data, system prompts, and template settings.
"""

# ──────────────────────────────────────────
# Employee List
# Updated from master spreadsheet 2026-02-20
# ──────────────────────────────────────────

CONFIG_EMPLOYEES_VERSION = 2

EMPLOYEES = [
    {"name": "Alexia Trejo", "email": "alexia.trejo@brite.co", "birthday_month": 4, "birthday_day": 10, "department": "", "title": "Underwriting Assistant"},
    {"name": "Anna Sotelo", "email": "anna.sotelo@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Account Executive"},
    {"name": "Azim Usmanov", "email": "Azim.Usmanov@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Technology Intern"},
    {"name": "Ben Glispie", "email": "ben.glispie@brite.co", "birthday_month": 2, "birthday_day": 1, "department": "", "title": "National Agent Channel Lead"},
    {"name": "Ben Mautner", "email": "ben.mautner@brite.co", "birthday_month": 8, "birthday_day": 17, "department": "", "title": "CIO"},
    {"name": "Brenno Cardoso", "email": "brenno.cardoso@brite.co", "birthday_month": 8, "birthday_day": 17, "department": "", "title": "Marketing Tech Specialist"},
    {"name": "Brian Babor", "email": "Brian.Babor@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "SEO/GEO Growth Assistant"},
    {"name": "Brian Kelly", "email": "brian.kelly@brite.co", "birthday_month": 7, "birthday_day": 7, "department": "", "title": "Account Executive"},
    {"name": "Cameron Chapman", "email": "cameron.chapman@brite.co", "birthday_month": 2, "birthday_day": 20, "department": "", "title": "Underwriting Assistant"},
    {"name": "Chane Walraven", "email": "chane.walraven@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Support Staff"},
    {"name": "Christine Belling", "email": "christine.belling@brite.co", "birthday_month": 6, "birthday_day": 19, "department": "", "title": "Director of Customer Success"},
    {"name": "Conor Redmond", "email": "conor.redmond@brite.co", "birthday_month": 3, "birthday_day": 30, "department": "", "title": "Chief Actuary"},
    {"name": "Dana Koutnik", "email": "dana.koutnik@brite.co", "birthday_month": 7, "birthday_day": 31, "department": "", "title": "Jeweler Innovation and Partner Success Manager"},
    {"name": "Darel Jevens", "email": "Darel.Jevens@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Content Writing / Editor"},
    {"name": "Dove Tugadi", "email": "dove@brite.co", "birthday_month": 12, "birthday_day": 20, "department": "", "title": "Executive Assistant"},
    {"name": "Dustin Lemick", "email": "dustin.lemick@brite.co", "birthday_month": 2, "birthday_day": 13, "department": "", "title": "CEO"},
    {"name": "Dustin Sitar", "email": "dustin.sitar@brite.co", "birthday_month": 4, "birthday_day": 23, "department": "", "title": "Head of Growth & Marketing"},
    {"name": "Dylanne Crugnale", "email": "dylanne.crugnale@brite.co", "birthday_month": 4, "birthday_day": 5, "department": "", "title": "Director of Strategic Marketing"},
    {"name": "Edgar Diengdoh", "email": "edgar.diengdoh@brite.co", "birthday_month": 2, "birthday_day": 27, "department": "", "title": "SEO & Marketing Manager"},
    {"name": "Ellen Zeff", "email": "Ellen.Zeff@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Appraiser"},
    {"name": "Ellie Asiuras", "email": "ellie.asiuras@brite.co", "birthday_month": 8, "birthday_day": 29, "department": "", "title": "Underwriting Manager"},
    {"name": "Esmeralda Galvan", "email": "esme.galvan@brite.co", "birthday_month": 11, "birthday_day": 14, "department": "", "title": "Underwriter"},
    {"name": "Hannah Greene-Gretzinger", "email": "hannah.greene-gretzinger@brite.co", "birthday_month": 11, "birthday_day": 19, "department": "", "title": "Marketing Specialist Organic & Paid Social"},
    {"name": "Jack Courtad", "email": "jack.courtad@brite.co", "birthday_month": 4, "birthday_day": 26, "department": "", "title": "Digital Marketing Intern"},
    {"name": "Jeff Felix", "email": "jeff.felix@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Account Executive"},
    {"name": "John Ortbal", "email": "john.ortbal@brite.co", "birthday_month": 11, "birthday_day": 13, "department": "", "title": "Senior Advisor"},
    {"name": "Jomar Pabua", "email": "jomar.pabua@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Assistant"},
    {"name": "Juannette Van der Walt", "email": "juannette.vanderwalt@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Support Staff"},
    {"name": "Kaitlyn Rigdon", "email": "kaitlyn.rigdon@brite.co", "birthday_month": 11, "birthday_day": 3, "department": "", "title": "Claims/Fulfillment Team Lead"},
    {"name": "Kevin Walters", "email": "kevin.walters@brite.co", "birthday_month": 11, "birthday_day": 20, "department": "", "title": "Senior Systems Engineer"},
    {"name": "Lauren Appenfeller", "email": "lauren.appenfeller@brite.co", "birthday_month": 11, "birthday_day": 11, "department": "", "title": "Digital Media Specialist"},
    {"name": "Madisyn Kafer", "email": "madisyn.kafer@brite.co", "birthday_month": 9, "birthday_day": 5, "department": "", "title": "Underwriting Assistant"},
    {"name": "Mardon Ballares", "email": "marlon.ballares@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Assistant"},
    {"name": "Mark Machan", "email": "mark.machan@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Account Executive"},
    {"name": "Marlon Ballares", "email": "marlon.ballares@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Support Staff"},
    {"name": "Mick Dela Cruz", "email": "Mick.DelaCruz@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Business Development Representative"},
    {"name": "Paxton Washington", "email": "paxton.washington@brite.co", "birthday_month": 5, "birthday_day": 22, "department": "", "title": "Jewelry Specialist in Underwriting"},
    {"name": "Rachel Akmakjian", "email": "rachel.akmakjian@brite.co", "birthday_month": 2, "birthday_day": 24, "department": "", "title": "Growth & Partnership Manager"},
    {"name": "Ryan McQuilkin", "email": "ryan.mcquilkin@brite.co", "birthday_month": 2, "birthday_day": 21, "department": "", "title": "Account Executive"},
    {"name": "Ryan Ponce", "email": "ryan.ponce@brite.co", "birthday_month": 10, "birthday_day": 1, "department": "", "title": "Account Executive"},
    {"name": "Saba Patel", "email": "saba.patel@brite.co", "birthday_month": 7, "birthday_day": 13, "department": "", "title": "Sales Assistant"},
    {"name": "Sabrina Lin", "email": "sabrina.lin@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Appraiser"},
    {"name": "Sam MacGregor", "email": "sam.macgregor@brite.co", "birthday_month": 5, "birthday_day": 11, "department": "", "title": "Senior Sales Operation Manager"},
    {"name": "Selena Fragassi", "email": "selena.fragassi@brite.co", "birthday_month": 5, "birthday_day": 8, "department": "", "title": "Content Editor"},
    {"name": "Sheena Sims", "email": "sheena.sims@brite.co", "birthday_month": 10, "birthday_day": 9, "department": "", "title": "Customer Service Representative"},
    {"name": "Stephanie Block", "email": "stephanie.block@brite.co", "birthday_month": 6, "birthday_day": 20, "department": "", "title": "Customer Experience Team Lead"},
    {"name": "Stephanie Lynn", "email": "stephanie.lynn@brite.co", "birthday_month": 1, "birthday_day": 19, "department": "", "title": "Project Manager"},
    {"name": "Tania Figueroa", "email": "tania.figueroa@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Creative"},
    {"name": "Teagyn Lindley", "email": "teagyn.lindley@brite.co", "birthday_month": 8, "birthday_day": 25, "department": "", "title": "Claims Coordinator"},
    {"name": "Tiffany Zhou", "email": "tiffany.zhou@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Appraiser"},
    {"name": "Vivian Aufang", "email": "vivian.aufang@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Appraiser"},
    {"name": "Yianni Asiuras", "email": "Yianni.Asiuras@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Assistant"},
    {"name": "Zayaan Rahman", "email": "zayaan.rahman@brite.co", "birthday_month": 0, "birthday_day": 0, "department": "", "title": "Underwriting Support Staff"},
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
        "- Each joke MUST be in setup | punchline format, separated by a pipe character (|)\n"
        "- Example format: Why did the diamond go to school? | Because it wanted more carats!\n"
        "- The setup is a question or statement, the punchline is the answer/payoff\n"
        "- Puns about jewelry, weddings, watches, or insurance are great\n"
        "- Keep it office-appropriate and upbeat\n"
        "- Number them 1, 2, 3 so the user can pick their favorite\n"
        "- Seasonal tie-ins for {month} are welcome\n\n"
        "Return ONLY the 3 numbered jokes in setup | punchline format, nothing else."
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
    "generate_game": (
        "Generate a {game_type} game/puzzle for an internal company newsletter at BriteCo (a jewelry insurance company).\n\n"
        "Month: {month}\n"
        "Optional context for inspiration: {context}\n\n"
        "IMPORTANT RULES:\n"
        "- Questions should be about GENERAL jewelry knowledge, gemstone facts, watch trivia, insurance concepts, "
        "or fun BriteCo-related topics — NOT about the current newsletter content.\n"
        "- Make questions moderately challenging — not trivia anyone could guess, but fun brain-teasers that "
        "make people think. Include interesting facts employees might not know.\n"
        "- For trivia, make wrong options plausible (not obviously fake).\n\n"
        "Game type instructions:\n"
        "- word_scramble: Scramble the letters of a jewelry/gemstone/insurance word. Make it 7+ letters. "
        "Return as JSON: {{\"scrambled\": \"MABERDCL\", \"hint\": \"A short hint\", \"answer\": \"BRACELET\"}}\n"
        "- trivia: Write 3-4 multiple choice trivia questions about jewelry, gemstones, watches, or insurance. "
        "Make them challenging but fun. "
        "Return as JSON: {{\"questions\": [{{\"q\": \"Question?\", \"options\": [\"A\", \"B\", \"C\", \"D\"], \"answer\": \"B\"}}]}}\n"
        "- emoji_rebus: Create an emoji sequence that represents a jewelry/gemstone/watch-related phrase. "
        "Return as JSON: {{\"emojis\": \"emoji sequence here\", \"hint\": \"A hint\", \"answer\": \"The phrase\"}}\n"
        "- fill_blank: Write 3-4 fill-in-the-blank sentences about jewelry, gemstones, or insurance facts. "
        "Return as JSON: {{\"blanks\": [{{\"sentence\": \"The ___ is the hardest natural substance on Earth\", \"answer\": \"diamond\"}}]}}\n"
        "- hidden_word: Create a 10x10 letter grid containing a hidden word related to jewelry/gems. "
        "Return as JSON: {{\"grid\": [[\"A\",\"B\",...]], \"word\": \"DIAMOND\", \"hint\": \"A precious stone\"}}\n\n"
        "IMPORTANT: Return ONLY valid JSON, no extra text or markdown."
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
