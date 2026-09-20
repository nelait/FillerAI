"""Source data for generation.

Everything here is public reference data (place names, common given and
family names, dictionary words). No record in this module describes a real
person, and nothing from a customer's environment is ever needed to run it,
which is the point: the tool has to work where real form data cannot go.

Geography is stored as coherent tuples rather than parallel lists, so a
generated city, state, ZIP and area code always belong together.
"""

from __future__ import annotations

from typing import NamedTuple


class Place(NamedTuple):
    city: str
    state: str  # USPS code
    state_name: str
    zip_codes: tuple[str, ...]
    area_codes: tuple[str, ...]
    timezone: str


PLACES: tuple[Place, ...] = (
    Place("New York", "NY", "New York", ("10001", "10011", "10022", "10128"), ("212", "646", "917"), "America/New_York"),
    Place("Brooklyn", "NY", "New York", ("11201", "11215", "11238"), ("718", "347"), "America/New_York"),
    Place("Buffalo", "NY", "New York", ("14201", "14214", "14222"), ("716",), "America/New_York"),
    Place("Los Angeles", "CA", "California", ("90012", "90026", "90064"), ("213", "323", "310"), "America/Los_Angeles"),
    Place("San Francisco", "CA", "California", ("94102", "94110", "94117"), ("415", "628"), "America/Los_Angeles"),
    Place("San Diego", "CA", "California", ("92101", "92103", "92117"), ("619", "858"), "America/Los_Angeles"),
    Place("Sacramento", "CA", "California", ("95814", "95818", "95825"), ("916",), "America/Los_Angeles"),
    Place("Chicago", "IL", "Illinois", ("60601", "60614", "60647"), ("312", "773"), "America/Chicago"),
    Place("Springfield", "IL", "Illinois", ("62701", "62704"), ("217",), "America/Chicago"),
    Place("Houston", "TX", "Texas", ("77002", "77006", "77019"), ("713", "281", "832"), "America/Chicago"),
    Place("Austin", "TX", "Texas", ("78701", "78704", "78723"), ("512",), "America/Chicago"),
    Place("Dallas", "TX", "Texas", ("75201", "75204", "75219"), ("214", "469", "972"), "America/Chicago"),
    Place("San Antonio", "TX", "Texas", ("78205", "78209", "78212"), ("210",), "America/Chicago"),
    Place("Phoenix", "AZ", "Arizona", ("85003", "85016", "85028"), ("602", "480"), "America/Phoenix"),
    Place("Tucson", "AZ", "Arizona", ("85701", "85719"), ("520",), "America/Phoenix"),
    Place("Philadelphia", "PA", "Pennsylvania", ("19102", "19123", "19147"), ("215", "267"), "America/New_York"),
    Place("Pittsburgh", "PA", "Pennsylvania", ("15213", "15222", "15232"), ("412",), "America/New_York"),
    Place("Miami", "FL", "Florida", ("33101", "33131", "33139"), ("305", "786"), "America/New_York"),
    Place("Orlando", "FL", "Florida", ("32801", "32803", "32819"), ("407", "321"), "America/New_York"),
    Place("Tampa", "FL", "Florida", ("33602", "33606", "33611"), ("813",), "America/New_York"),
    Place("Jacksonville", "FL", "Florida", ("32202", "32204", "32207"), ("904",), "America/New_York"),
    Place("Atlanta", "GA", "Georgia", ("30303", "30308", "30318"), ("404", "678", "470"), "America/New_York"),
    Place("Savannah", "GA", "Georgia", ("31401", "31405"), ("912",), "America/New_York"),
    Place("Boston", "MA", "Massachusetts", ("02108", "02116", "02135"), ("617", "857"), "America/New_York"),
    Place("Worcester", "MA", "Massachusetts", ("01602", "01609"), ("508", "774"), "America/New_York"),
    Place("Seattle", "WA", "Washington", ("98101", "98109", "98122"), ("206",), "America/Los_Angeles"),
    Place("Spokane", "WA", "Washington", ("99201", "99205"), ("509",), "America/Los_Angeles"),
    Place("Denver", "CO", "Colorado", ("80202", "80205", "80218"), ("303", "720"), "America/Denver"),
    Place("Colorado Springs", "CO", "Colorado", ("80903", "80909"), ("719",), "America/Denver"),
    Place("Portland", "OR", "Oregon", ("97201", "97209", "97214"), ("503", "971"), "America/Los_Angeles"),
    Place("Eugene", "OR", "Oregon", ("97401", "97405"), ("541",), "America/Los_Angeles"),
    Place("Detroit", "MI", "Michigan", ("48201", "48226"), ("313",), "America/New_York"),
    Place("Ann Arbor", "MI", "Michigan", ("48103", "48104"), ("734",), "America/New_York"),
    Place("Minneapolis", "MN", "Minnesota", ("55401", "55408", "55414"), ("612",), "America/Chicago"),
    Place("Saint Paul", "MN", "Minnesota", ("55102", "55104"), ("651",), "America/Chicago"),
    Place("Charlotte", "NC", "North Carolina", ("28202", "28204", "28209"), ("704", "980"), "America/New_York"),
    Place("Raleigh", "NC", "North Carolina", ("27601", "27605", "27609"), ("919", "984"), "America/New_York"),
    Place("Nashville", "TN", "Tennessee", ("37201", "37203", "37212"), ("615",), "America/Chicago"),
    Place("Memphis", "TN", "Tennessee", ("38103", "38104"), ("901",), "America/Chicago"),
    Place("Columbus", "OH", "Ohio", ("43215", "43201", "43206"), ("614",), "America/New_York"),
    Place("Cleveland", "OH", "Ohio", ("44113", "44114"), ("216",), "America/New_York"),
    Place("Cincinnati", "OH", "Ohio", ("45202", "45220"), ("513",), "America/New_York"),
    Place("Indianapolis", "IN", "Indiana", ("46204", "46202", "46220"), ("317",), "America/New_York"),
    Place("Kansas City", "MO", "Missouri", ("64106", "64111"), ("816",), "America/Chicago"),
    Place("Saint Louis", "MO", "Missouri", ("63101", "63108"), ("314",), "America/Chicago"),
    Place("Milwaukee", "WI", "Wisconsin", ("53202", "53211"), ("414",), "America/Chicago"),
    Place("Madison", "WI", "Wisconsin", ("53703", "53705"), ("608",), "America/Chicago"),
    Place("Las Vegas", "NV", "Nevada", ("89101", "89109", "89135"), ("702", "725"), "America/Los_Angeles"),
    Place("Salt Lake City", "UT", "Utah", ("84101", "84102", "84111"), ("801", "385"), "America/Denver"),
    Place("Albuquerque", "NM", "New Mexico", ("87102", "87106"), ("505",), "America/Denver"),
    Place("Oklahoma City", "OK", "Oklahoma", ("73102", "73106"), ("405",), "America/Chicago"),
    Place("New Orleans", "LA", "Louisiana", ("70112", "70130"), ("504",), "America/Chicago"),
    Place("Baltimore", "MD", "Maryland", ("21201", "21230"), ("410", "443"), "America/New_York"),
    Place("Richmond", "VA", "Virginia", ("23219", "23220"), ("804",), "America/New_York"),
    Place("Virginia Beach", "VA", "Virginia", ("23451", "23454"), ("757",), "America/New_York"),
    Place("Newark", "NJ", "New Jersey", ("07102", "07105"), ("973",), "America/New_York"),
    Place("Jersey City", "NJ", "New Jersey", ("07302", "07306"), ("201", "551"), "America/New_York"),
    Place("Hartford", "CT", "Connecticut", ("06103", "06106"), ("860",), "America/New_York"),
    Place("Providence", "RI", "Rhode Island", ("02903", "02906"), ("401",), "America/New_York"),
    Place("Boise", "ID", "Idaho", ("83702", "83706"), ("208",), "America/Denver"),
    Place("Des Moines", "IA", "Iowa", ("50309", "50312"), ("515",), "America/Chicago"),
    Place("Omaha", "NE", "Nebraska", ("68102", "68105"), ("402",), "America/Chicago"),
    Place("Little Rock", "AR", "Arkansas", ("72201", "72205"), ("501",), "America/Chicago"),
    Place("Birmingham", "AL", "Alabama", ("35203", "35205"), ("205",), "America/Chicago"),
    Place("Charleston", "SC", "South Carolina", ("29401", "29403"), ("843",), "America/New_York"),
    Place("Louisville", "KY", "Kentucky", ("40202", "40204"), ("502",), "America/New_York"),
    Place("Honolulu", "HI", "Hawaii", ("96813", "96815"), ("808",), "Pacific/Honolulu"),
    Place("Anchorage", "AK", "Alaska", ("99501", "99503"), ("907",), "America/Anchorage"),
    Place("Portland", "ME", "Maine", ("04101", "04102"), ("207",), "America/New_York"),
    Place("Burlington", "VT", "Vermont", ("05401", "05403"), ("802",), "America/New_York"),
    Place("Manchester", "NH", "New Hampshire", ("03101", "03104"), ("603",), "America/New_York"),
    Place("Wilmington", "DE", "Delaware", ("19801", "19806"), ("302",), "America/New_York"),
    Place("Washington", "DC", "District of Columbia", ("20001", "20009", "20037"), ("202",), "America/New_York"),
    Place("Billings", "MT", "Montana", ("59101", "59102"), ("406",), "America/Denver"),
    Place("Fargo", "ND", "North Dakota", ("58102", "58103"), ("701",), "America/Chicago"),
    Place("Sioux Falls", "SD", "South Dakota", ("57104", "57105"), ("605",), "America/Chicago"),
    Place("Cheyenne", "WY", "Wyoming", ("82001", "82009"), ("307",), "America/Denver"),
    Place("Wichita", "KS", "Kansas", ("67202", "67208"), ("316",), "America/Chicago"),
    Place("Jackson", "MS", "Mississippi", ("39201", "39211"), ("601",), "America/Chicago"),
    Place("Charleston", "WV", "West Virginia", ("25301", "25302"), ("304",), "America/New_York"),
)

STATE_NAMES: dict[str, str] = {p.state: p.state_name for p in PLACES}

COUNTRIES = (
    ("US", "United States"),
    ("CA", "Canada"),
    ("GB", "United Kingdom"),
    ("AU", "Australia"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("IN", "India"),
    ("JP", "Japan"),
    ("BR", "Brazil"),
    ("MX", "Mexico"),
)

FIRST_NAMES_NEUTRAL = (
    "Avery", "Riley", "Jordan", "Taylor", "Morgan", "Casey", "Quinn", "Rowan",
    "Skyler", "Emerson", "Finley", "Harper", "Sage", "Reese", "Dakota",
    "Alexis", "Jamie", "Cameron", "Devon", "Elliot", "Hayden", "Kendall",
    "Logan", "Parker", "Peyton", "Remy", "River", "Shea", "Sydney", "Tatum",
)

FIRST_NAMES_COMMON = (
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Christopher", "Karen", "Daniel",
    "Nancy", "Matthew", "Lisa", "Anthony", "Margaret", "Mark", "Betty",
    "Ana", "Wei", "Priya", "Omar", "Yuki", "Ivan", "Fatima", "Diego",
    "Leila", "Sanjay", "Mei", "Kofi", "Nadia", "Tomas", "Aisha", "Hiroshi",
)

LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Patel", "Kim", "Chen", "Okafor", "Ahmed",
    "Kowalski", "Novak", "Dubois", "Rossi", "Fernandez", "Silva", "Murphy",
)

NAME_PREFIXES = ("Mr.", "Ms.", "Mrs.", "Dr.", "Mx.", "Prof.")

# Which honorifics go with which gender value, so the two agree on a record.
# Titles that carry no gender are available to everyone.
GENDER_NEUTRAL_PREFIXES = ("Dr.", "Prof.", "Mx.")
PREFIXES_BY_GENDER = {
    "Female": ("Ms.", "Mrs.") + GENDER_NEUTRAL_PREFIXES,
    "Male": ("Mr.",) + GENDER_NEUTRAL_PREFIXES,
    "Non-binary": GENDER_NEUTRAL_PREFIXES,
    "Prefer not to say": GENDER_NEUTRAL_PREFIXES,
}
NAME_SUFFIXES = ("Jr.", "Sr.", "II", "III", "PhD", "MD")

STREET_NAMES = (
    "Maple", "Oak", "Cedar", "Pine", "Elm", "Washington", "Lincoln", "Park",
    "Lake", "Hill", "Sunset", "Ridge", "Willow", "Birch", "Chestnut",
    "Franklin", "Jefferson", "Madison", "Monroe", "Adams", "Highland",
    "Spring", "River", "Meadow", "Orchard", "Prospect", "Union", "Market",
    "Church", "Center", "Bridge", "Mill", "Forest", "Valley", "Summit",
)

STREET_TYPES = ("St", "Ave", "Rd", "Dr", "Ln", "Blvd", "Ct", "Way", "Pl", "Ter")

SECONDARY_UNITS = ("Apt", "Suite", "Unit", "#", "Bldg", "Fl")

COMPANY_HEADS = (
    "Northwind", "Contoso", "Fabrikam", "Adventure", "Blue Harbor",
    "Cedar Ridge", "Granite", "Lakeside", "Meridian", "Northstar",
    "Pinnacle", "Redwood", "Silverline", "Summit", "Trailhead", "Vantage",
    "Westbrook", "Ironwood", "Clearwater", "Keystone", "Beacon", "Brightpath",
)

COMPANY_TAILS = (
    "Logistics", "Systems", "Industries", "Partners", "Group", "Holdings",
    "Solutions", "Services", "Technologies", "Labs", "Associates", "Supply",
    "Manufacturing", "Consulting", "Financial", "Health", "Insurance",
)

COMPANY_SUFFIXES = ("Inc.", "LLC", "Corp.", "Co.", "Ltd.", "LLP")

JOB_TITLES = (
    "Claims Adjuster", "Account Manager", "Operations Analyst",
    "Customer Service Representative", "Underwriter", "Field Technician",
    "Registered Nurse", "Project Manager", "Software Engineer", "Accountant",
    "Logistics Coordinator", "Quality Inspector", "Store Manager",
    "Administrative Assistant", "Sales Associate", "Data Analyst",
    "Human Resources Specialist", "Warehouse Supervisor", "Paralegal",
    "Benefits Coordinator", "Billing Specialist", "Case Worker",
)

DEPARTMENTS = (
    "Claims", "Underwriting", "Operations", "Finance", "Customer Service",
    "Human Resources", "Information Technology", "Legal", "Sales",
    "Compliance", "Facilities", "Procurement", "Marketing", "Engineering",
)

# Domains reserved by RFC 2606/6761 for documentation and testing, so a
# generated address can never reach a real mailbox.
EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net", "test.example",
    "mail.example.com", "corp.example.net",
)

GENDERS = ("Female", "Male", "Non-binary", "Prefer not to say")

# Real ICD-10 category codes with their descriptions, useful for claim forms.
DIAGNOSIS_CODES = (
    ("M54.5", "Low back pain"),
    ("J06.9", "Acute upper respiratory infection, unspecified"),
    ("E11.9", "Type 2 diabetes mellitus without complications"),
    ("I10", "Essential (primary) hypertension"),
    ("R51.9", "Headache, unspecified"),
    ("S93.401A", "Sprain of unspecified ligament of right ankle"),
    ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
    ("F41.1", "Generalized anxiety disorder"),
    ("J45.909", "Unspecified asthma, uncomplicated"),
    ("M25.561", "Pain in right knee"),
)

LOREM_SENTENCES = (
    "Customer reported the issue during the scheduled follow-up call.",
    "Additional documentation was requested and is pending review.",
    "The account was verified against the records on file.",
    "No prior claims were found under this policy number.",
    "Coverage details were confirmed with the provider directly.",
    "The applicant requested a callback later in the week.",
    "Supporting receipts were uploaded to the case record.",
    "A supervisor approved the adjustment after review.",
    "The address on file was updated at the customer's request.",
    "Further inspection is scheduled for the following business day.",
)
