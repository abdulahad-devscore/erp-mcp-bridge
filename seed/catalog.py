"""Synthetic master data for Tembusu Housewares Sdn Bhd.

Tembusu is a fictional mid-sized Malaysian housewares company used across
all three showcase repos. It covers the three industries we sell into:

* Manufacturing: a factory in Shah Alam turns aluminium, steel and resin into
  cookware, bakeware and kitchen storage (bills of materials below).
* Logistics: a distribution centre in Port Klang feeds three retail stores
  and the online channel; replenishment is warehouse-to-warehouse transfers.
* Retail: own stores (KLCC, Petaling Jaya, Penang), an online shop, and
  wholesale customers (supermarkets, kitchen shops, hotels).

Every name here is invented. Any resemblance to a real business is accidental.
"""

COMPANY = {
    "name": "Tembusu Housewares Sdn Bhd",
    "street": "Lot 12, Jalan Perindustrian 7",
    "city": "Shah Alam",
    "zip": "40000",
    "email": "ops@tembusu-housewares.example",
}

# code, name, role. Odoo limits warehouse codes to 5 characters.
WAREHOUSES = [
    ("FAC", "Shah Alam Factory", "factory"),
    ("DC", "Port Klang Distribution Centre", "dc"),
    ("KLCC", "Store - KLCC", "store"),
    ("PJ", "Store - Petaling Jaya", "store"),
    ("PNG", "Store - Penang", "store"),
]

CATEGORIES = ["Cookware", "Bakeware", "Kitchen Storage", "Tableware", "Small Appliances", "Raw Materials"]

# sku, name, category, list_price, cost, source ("make" or "buy")
FINISHED_GOODS = [
    ("TH-CW-2401", "Cast Aluminium Wok 32cm", "Cookware", 129.00, 52.00, "make"),
    ("TH-CW-2402", "Cast Aluminium Wok 36cm", "Cookware", 149.00, 61.00, "make"),
    ("TH-CW-2410", "Non-stick Frying Pan 24cm", "Cookware", 79.00, 28.00, "make"),
    ("TH-CW-2411", "Non-stick Frying Pan 28cm", "Cookware", 95.00, 34.00, "make"),
    ("TH-CW-2420", "Stockpot 24cm with Glass Lid", "Cookware", 159.00, 63.00, "make"),
    ("TH-CW-2421", "Stockpot 28cm with Glass Lid", "Cookware", 189.00, 76.00, "make"),
    ("TH-CW-2430", "Saucepan 18cm", "Cookware", 69.00, 25.00, "make"),
    ("TH-CW-2440", "Grill Pan 28cm", "Cookware", 109.00, 41.00, "make"),
    ("TH-BW-3101", "Loaf Tin 900g", "Bakeware", 29.90, 9.50, "make"),
    ("TH-BW-3102", "Round Cake Tin 20cm", "Bakeware", 34.90, 11.00, "make"),
    ("TH-BW-3103", "Muffin Tray 12-cup", "Bakeware", 39.90, 13.50, "make"),
    ("TH-BW-3104", "Baking Tray 40x30cm", "Bakeware", 32.90, 10.20, "make"),
    ("TH-KS-4201", "Airtight Container 0.8L", "Kitchen Storage", 12.90, 3.60, "make"),
    ("TH-KS-4202", "Airtight Container 1.5L", "Kitchen Storage", 16.90, 4.80, "make"),
    ("TH-KS-4203", "Airtight Container 3.0L", "Kitchen Storage", 22.90, 6.90, "make"),
    ("TH-KS-4210", "Rice Dispenser 10kg", "Kitchen Storage", 69.00, 22.00, "make"),
    ("TH-KS-4220", "Lunch Box 3-compartment", "Kitchen Storage", 19.90, 5.40, "make"),
    ("TH-KS-4230", "Spice Rack 12-jar", "Kitchen Storage", 49.90, 16.80, "make"),
    ("TH-TW-5101", "Melamine Dinner Plate 10in", "Tableware", 8.90, 3.10, "buy"),
    ("TH-TW-5102", "Melamine Bowl 6in", "Tableware", 6.90, 2.40, "buy"),
    ("TH-TW-5110", "Stainless Cutlery Set 24pc", "Tableware", 59.90, 21.00, "buy"),
    ("TH-TW-5120", "Glass Tumbler Set of 6", "Tableware", 29.90, 10.50, "buy"),
    ("TH-SA-6101", "Electric Kettle 1.7L", "Small Appliances", 89.00, 48.00, "buy"),
    ("TH-SA-6102", "Rice Cooker 1.8L", "Small Appliances", 159.00, 88.00, "buy"),
    ("TH-SA-6103", "Hand Blender 300W", "Small Appliances", 119.00, 62.00, "buy"),
    ("TH-SA-6104", "Air Fryer 4.5L", "Small Appliances", 299.00, 171.00, "buy"),
]

# sku, name, uom, cost
RAW_MATERIALS = [
    ("RM-AL-INGOT", "Aluminium Ingot A356", "kg", 11.20),
    ("RM-NS-COAT", "Non-stick Coating (PTFE, 2-layer)", "L", 38.00),
    ("RM-HDL-BAK", "Bakelite Handle", "Units", 2.10),
    ("RM-LID-GL24", "Tempered Glass Lid 24cm", "Units", 6.80),
    ("RM-LID-GL28", "Tempered Glass Lid 28cm", "Units", 7.90),
    ("RM-STL-SHEET", "Carbon Steel Sheet 0.8mm", "kg", 5.40),
    ("RM-PP-RESIN", "Polypropylene Resin (food grade)", "kg", 6.20),
    ("RM-SIL-SEAL", "Silicone Seal Ring", "Units", 0.35),
    ("RM-PKG-BOX", "Printed Carton", "Units", 0.90),
    ("RM-PKG-SLV", "Printed Sleeve", "Units", 0.25),
]

# finished sku -> [(component sku, qty per unit)]
BOMS = {
    "TH-CW-2401": [("RM-AL-INGOT", 1.6), ("RM-NS-COAT", 0.05), ("RM-HDL-BAK", 2), ("RM-PKG-BOX", 1)],
    "TH-CW-2402": [("RM-AL-INGOT", 2.0), ("RM-NS-COAT", 0.06), ("RM-HDL-BAK", 2), ("RM-PKG-BOX", 1)],
    "TH-CW-2410": [("RM-AL-INGOT", 0.7), ("RM-NS-COAT", 0.03), ("RM-HDL-BAK", 1), ("RM-PKG-SLV", 1)],
    "TH-CW-2411": [("RM-AL-INGOT", 0.9), ("RM-NS-COAT", 0.04), ("RM-HDL-BAK", 1), ("RM-PKG-SLV", 1)],
    "TH-CW-2420": [("RM-AL-INGOT", 1.4), ("RM-LID-GL24", 1), ("RM-HDL-BAK", 2), ("RM-PKG-BOX", 1)],
    "TH-CW-2421": [("RM-AL-INGOT", 1.8), ("RM-LID-GL28", 1), ("RM-HDL-BAK", 2), ("RM-PKG-BOX", 1)],
    "TH-CW-2430": [("RM-AL-INGOT", 0.6), ("RM-NS-COAT", 0.02), ("RM-HDL-BAK", 1), ("RM-PKG-SLV", 1)],
    "TH-CW-2440": [("RM-AL-INGOT", 1.1), ("RM-NS-COAT", 0.04), ("RM-HDL-BAK", 1), ("RM-PKG-BOX", 1)],
    "TH-BW-3101": [("RM-STL-SHEET", 0.45), ("RM-NS-COAT", 0.02), ("RM-PKG-SLV", 1)],
    "TH-BW-3102": [("RM-STL-SHEET", 0.5), ("RM-NS-COAT", 0.02), ("RM-PKG-SLV", 1)],
    "TH-BW-3103": [("RM-STL-SHEET", 0.7), ("RM-NS-COAT", 0.03), ("RM-PKG-SLV", 1)],
    "TH-BW-3104": [("RM-STL-SHEET", 0.9), ("RM-NS-COAT", 0.03), ("RM-PKG-SLV", 1)],
    "TH-KS-4201": [("RM-PP-RESIN", 0.09), ("RM-SIL-SEAL", 1), ("RM-PKG-SLV", 1)],
    "TH-KS-4202": [("RM-PP-RESIN", 0.14), ("RM-SIL-SEAL", 1), ("RM-PKG-SLV", 1)],
    "TH-KS-4203": [("RM-PP-RESIN", 0.22), ("RM-SIL-SEAL", 1), ("RM-PKG-SLV", 1)],
    "TH-KS-4210": [("RM-PP-RESIN", 1.2), ("RM-PKG-BOX", 1)],
    "TH-KS-4220": [("RM-PP-RESIN", 0.18), ("RM-SIL-SEAL", 3), ("RM-PKG-SLV", 1)],
    "TH-KS-4230": [("RM-PP-RESIN", 0.6), ("RM-PKG-BOX", 1)],
}

# name, ref, payment term days, supplies [(sku, unit price, lead days)]
SUPPLIERS = [
    ("Perak Alloy Supplies Sdn Bhd", "SUP-PAS", 30, [("RM-AL-INGOT", 11.20, 7)]),
    ("Selangor Coatings Sdn Bhd", "SUP-SCS", 30, [("RM-NS-COAT", 38.00, 10)]),
    (
        "Klang Valley Plastics Sdn Bhd",
        "SUP-KVP",
        30,
        [("RM-PP-RESIN", 6.20, 5), ("RM-SIL-SEAL", 0.35, 5), ("RM-HDL-BAK", 2.10, 7)],
    ),
    (
        "Johor Glassworks Sdn Bhd",
        "SUP-JGW",
        45,
        [("RM-LID-GL24", 6.80, 14), ("RM-LID-GL28", 7.90, 14), ("TH-TW-5120", 10.50, 14)],
    ),
    ("Penang Packaging Sdn Bhd", "SUP-PPK", 30, [("RM-PKG-BOX", 0.90, 5), ("RM-PKG-SLV", 0.25, 5)]),
    ("Butterworth Steel Trading Sdn Bhd", "SUP-BST", 30, [("RM-STL-SHEET", 5.40, 7)]),
    (
        "Hengfa Tableware Trading Co., Ltd.",
        "SUP-HTT",
        60,
        [("TH-TW-5101", 3.10, 35), ("TH-TW-5102", 2.40, 35), ("TH-TW-5110", 21.00, 35)],
    ),
    (
        "Brightwave Home Appliances Co., Ltd.",
        "SUP-BHA",
        60,
        [("TH-SA-6101", 48.00, 40), ("TH-SA-6102", 88.00, 40), ("TH-SA-6103", 62.00, 40), ("TH-SA-6104", 171.00, 40)],
    ),
]

# name, ref, channel, payment term days, credit limit, pays late?, order size factor
CUSTOMERS = [
    ("Pasaraya Seri Mutiara Sdn Bhd", "CUS-PSM", "Wholesale", 30, 150000, False, 1.0),
    ("HomeMart Ipoh Sdn Bhd", "CUS-HMI", "Wholesale", 30, 90000, False, 0.8),
    ("Dapur Bonda Trading", "CUS-DBT", "Wholesale", 30, 20000, True, 0.25),
    ("Kuching Home Centre Sdn Bhd", "CUS-KHC", "Wholesale", 45, 60000, True, 0.6),
    ("Melaka Hardware & Kitchen Enterprise", "CUS-MHK", "Wholesale", 30, 40000, False, 0.45),
    ("Hotel Cempaka Kuala Lumpur", "CUS-HCK", "Wholesale", 30, 40000, False, 0.35),
    ("Restoran Selera Kampung Group", "CUS-RSK", "Wholesale", 14, 12000, True, 0.15),
    ("Borneo Living Retail Sdn Bhd", "CUS-BLR", "Wholesale", 45, 120000, False, 0.9),
    ("Online Store Customers", "CUS-WEB", "Online", 0, 0, False, 1.0),
    ("Walk-in Customer - KLCC", "CUS-KLCC", "Retail Stores", 0, 0, False, 1.0),
    ("Walk-in Customer - Petaling Jaya", "CUS-PJ", "Retail Stores", 0, 0, False, 1.0),
    ("Walk-in Customer - Penang", "CUS-PNG", "Retail Stores", 0, 0, False, 1.0),
]

SALES_TEAMS = ["Wholesale", "Online", "Retail Stores"]

# Stock on hand per warehouse. Stores deliberately sit below reorder minimum
# for a few SKUs so "what should we reorder?" has a real answer.
STORE_STOCK_PROFILE = {"KLCC": 1.0, "PJ": 0.8, "PNG": 0.6}

# min, max per store for finished goods (overridden for fast movers)
STORE_REORDER = {"default": (10, 40), "TH-KS-4201": (30, 120), "TH-KS-4202": (30, 120), "TH-TW-5101": (40, 160)}
DC_REORDER = {"default": (80, 400)}
FACTORY_RM_REORDER = {
    "RM-AL-INGOT": (2000, 8000),
    "RM-NS-COAT": (60, 250),
    "RM-HDL-BAK": (3000, 12000),
    "RM-LID-GL24": (300, 1200),
    "RM-LID-GL28": (300, 1200),
    "RM-STL-SHEET": (800, 3000),
    "RM-PP-RESIN": (1200, 5000),
    "RM-SIL-SEAL": (4000, 15000),
    "RM-PKG-BOX": (3000, 12000),
    "RM-PKG-SLV": (5000, 20000),
}
