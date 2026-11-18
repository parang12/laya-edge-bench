"""Additional AI4I source rows for demonstration, not an independent test set.

Rows are from ai4i2020.csv in ai4i+2020+predictive+maintenance+dataset.zip.
The five original demonstrations are excluded. Normal rows have no failure
flags; fault rows have Machine failure=1 and only the named failure flag.
The train/validation/test membership of these rows has not been established.
"""

# Each tuple: UDI, product type, air temperature K, process temperature K,
# rotational speed rpm, torque Nm, tool wear minutes.
ADDITIONAL_CASES = {
    "정상": (
        (44, "H", 298.8, 309.2, 1372, 48.5, 120),
        (347, "M", 297.5, 308.3, 1459, 45.2, 34),
        (5034, "L", 304.0, 313.1, 1564, 50.9, 119),
        (8170, "M", 299.8, 311.2, 1745, 26.3, 150),
    ),
    "열 방출 장애": (
        (4841, "L", 303.4, 311.9, 1298, 63.9, 59),
        (4185, "L", 302.2, 310.6, 1334, 53.2, 159),
        (4496, "L", 302.6, 310.4, 1359, 57.2, 67),
        (4449, "L", 302.4, 310.3, 1374, 51.2, 163),
    ),
    "동력 고장": (
        (5049, "H", 304.0, 313.2, 1271, 68.6, 161),
        (6679, "M", 301.5, 310.7, 1336, 65.6, 0),
        (904, "L", 295.7, 306.2, 2270, 14.6, 149),
        (3868, "M", 302.7, 311.6, 2709, 9.7, 2),
    ),
    "과부하 고장": (
        (7594, "L", 300.5, 311.2, 1270, 67.5, 208),
        (2582, "L", 299.2, 309.1, 1345, 60.7, 191),
        (328, "L", 297.7, 308.5, 1373, 56.7, 203),
        (5400, "H", 302.8, 312.4, 1411, 53.8, 246),
    ),
    "공구 마모 고장": (
        (6420, "H", 300.3, 309.9, 1394, 46.7, 210),
        (6257, "L", 301.0, 310.6, 1493, 37.8, 206),
        (1683, "H", 297.9, 307.4, 1604, 36.1, 225),
        (9175, "L", 297.8, 308.9, 1871, 25.6, 200),
    ),
}
