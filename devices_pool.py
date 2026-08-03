"""
devices_pool.py
───────────────
Pre-defined pool of real-world Android device fingerprints.

Each entry mirrors a physical device so the browser context looks
authentic to server-side fingerprinting checks.  The pool is used by
`utils.device_manager` when the sheet row doesn't specify a custom device.
"""

DEVICE_POOL: list[dict] = [
    # ── Google Pixel Series ──────────────────────────────────────────
    {
        "model": "Pixel 7",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Pixel 7 Pro",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 892},
        "device_scale_factor": 3.5,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Pixel 8",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 932},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Pixel 8 Pro",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 8 Pro) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 448, "height": 998},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Pixel 9",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 9) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 924},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },

    # ── Samsung Galaxy S Series ──────────────────────────────────────
    {
        "model": "Galaxy S23",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S911B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy S23 Ultra",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 824},
        "device_scale_factor": 3.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy S24",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; SM-S921B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Galaxy S24 Ultra",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; SM-S928B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 824},
        "device_scale_factor": 3.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },

    # ── Samsung Galaxy A Series ──────────────────────────────────────
    {
        "model": "Galaxy A54",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-A546B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy A15",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-A156B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 854},
        "device_scale_factor": 2.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── OnePlus ──────────────────────────────────────────────────────
    {
        "model": "OnePlus 12",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; CPH2583) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 919},
        "device_scale_factor": 3.5,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Xiaomi / Redmi ───────────────────────────────────────────────
    {
        "model": "Redmi Note 13 Pro",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; 2312DRA50G) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 393, "height": 873},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Xiaomi 14",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; 2311DRK48C) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 393, "height": 851},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Nothing / Motorola ───────────────────────────────────────────
    {
        "model": "Nothing Phone (2)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; A065) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Moto G84",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; motorola edge 40) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Samsung Galaxy — Samsung Browser ────────────────────────────
    {
        "model": "Galaxy S24 (Samsung Browser)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; SM-S921B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Galaxy S23 (Samsung Browser)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S911B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy A54 (Samsung Browser)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-A546B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Apple iPhone — Safari (iOS) ──────────────────────────────────
    {
        "model": "iPhone 16 Pro",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 402, "height": 874},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 16",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/129.0.6668.46 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 15 Pro",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 393, "height": 852},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 15",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/124.0.6367.82 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 14 Pro",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 393, "height": 852},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 14",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/121.0.6167.66 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 13",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/16.6 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 13 mini",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 375, "height": 812},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
]

# ── Extra real-world Android user agents (2025-2026 fleet) ────────────────────
# Each line below is turned into a DEVICE_POOL entry so the random picker can
# hand out a fresh, non-repeating UA per row.  Add more by pasting extra lines
# into the block — one full UA string per line.
import re as _re

_EXTRA_UA_BLOCK = r"""
Mozilla/5.0 (Linux; Android 16; SM-A166U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S931U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S908U Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S916U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S926U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S942U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S938U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-A166U1 Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 14; SM-P613 Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S911U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 12; SM-G991U1 Build/SP1A.210812.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S928U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 15; SM-G998U Build/AP3A.240905.015.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; TB336FU Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Safari/537.36
Dalvik/2.1.0 (Linux; U; Android 16; SM-S928U Build/BP2A.250605.031.A3),Mozilla/5.0 (Linux; Android 16; SM-S928U Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.192 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-X510 Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-F721U Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-A176U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; motorola edge 2025 Build/W1VDS36H.50-38-3-8; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-A536U Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 14; moto g power 5G - 2023 Build/U1TOS34.1-157-5-27; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 12; moto g stylus 5G Build/S2RE32.29-16-1-5-19; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; moto g - 2025 Build/W1VKS36H.9-12-10-5; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-F946U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Safari/537.36
Mozilla/5.0 (Linux; Android 12; CPH2119 Build/SP1A.210812.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/124.0.6367.179 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 15; moto g 5G - 2024 Build/V1UFNS35H.193-20-14; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S938U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.182 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 12; moto g (100)Build/S1RTS32.41-20-16-1-9; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 15; SM-A146U Build/AP3A.240905.015.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/148.0.7778.215 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 15; SM-G991U Build/AP3A.240905.015.A2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 9; SM-G950F Build/PPR1.180610.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/138.0.7204.179 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-A376U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 10; Redmi Note 9S Build/QKQ1.191215.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/147.0.7727.137 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 13; T432W Build/TP1A.220624.014; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 13; SM-G985F Build/TP1A.220624.014; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0.5359.128 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 8.0.0; SM-G930L Build/R16NW; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/138.0.7204.179 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 17; Pixel 10 Pro Build/CP2A.260705.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 11; 5062W Build/RKQ1.201202.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 15; moto g 5G - 2024 Build/V1UFNS35H.193-24-8; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S711U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 14; TECNO KL4 Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/147.0.7727.137 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 13; motorola edge 5G UW (2021)Build/T1RM33.1-110-17-8-2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S901U1 Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; moto g stylus - 2025 Build/W1VAS36.62-22-16-11; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.114 Mobile DramaBox/6.1.1
Mozilla/5.0 (Linux; Android 11; KFSNWI Build/RS8338.3339N; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/148.0.7778.258 Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-S921U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 12; 4188C Build/SP1A.210812.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 13; 100110603 Build/TP1A.220624.014; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Safari/537.36
"""


def _ua_to_device(ua: str) -> dict | None:
    ua = ua.strip()
    if not ua:
        return None
    # A couple of pasted lines prefix a Dalvik app-UA before the real browser
    # UA (joined by a comma) — keep only the browser (Mozilla/...) portion.
    if "Mozilla/5.0" in ua and not ua.startswith("Mozilla/5.0"):
        ua = ua[ua.index("Mozilla/5.0"):]
    # iOS — inject_stealth() already applies iPhone navigator hints when it sees
    # an iPhone/iPad UA, so these stay internally consistent.
    if "iPhone" in ua:
        return {"model": "iPhone", "user_agent": ua,
                "viewport": {"width": 390, "height": 844}, "device_scale_factor": 3.0,
                "is_mobile": True, "has_touch": True, "os": "ios", "android_version": ""}
    if "iPad" in ua:
        return {"model": "iPad", "user_agent": ua,
                "viewport": {"width": 820, "height": 1180}, "device_scale_factor": 2.0,
                "is_mobile": True, "has_touch": True, "os": "ios", "android_version": ""}
    # Android / Fire (Silk)
    ver = _re.search(r"Android (\d+(?:\.\d+)*)", ua)
    android = ver.group(1) if ver else "14"
    mdl = _re.search(r"Android [\d.]+; ([^;)]+?)(?:\s*Build|\)|;)", ua)
    model = mdl.group(1).strip() if mdl else "Android Device"
    # Tablets/foldables omit the "Mobile" token; give them a wider viewport.
    tablet = "Mobile" not in ua
    return {
        "model": model,
        "user_agent": ua,
        "viewport": {"width": 800, "height": 1280} if tablet else {"width": 412, "height": 915},
        "device_scale_factor": 2.0 if tablet else 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": android,
    }


# Second batch — iOS (iPhone/iPad), Fire Silk tablets, and more Android.
_EXTRA_UA_BLOCK_2 = r"""
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 9; KFTRWI) AppleWebKit/537.36 (KHTML, like Gecko) Silk/138.14.12 like Chrome/138.0.7204.244 Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.2 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/149.0.7827.137 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 11; KFTUWI) AppleWebKit/537.36 (KHTML, like Gecko) Silk/148.4.3 like Chrome/148.0.0.0 Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/150.0.7871.51 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/428.4.939275213 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 15_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.80 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_4_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/428.4.939275213 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/428.4.939275213 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 15; 25028RN03L Build/AP3A.240905.015.A2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.7390.41 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Mobile/23B85 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 16_7_15 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/358.1.731895952 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 18_3_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.2 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.1005 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/605.1.1005
Mozilla/5.0 (iPad; CPU OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/428.4.939275213 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_6_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 15_7_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/387.1.809473243 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/150.0.7871.113 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/150.0.7871.51 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 17_7_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/142.0.7444.46 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.98 (KHTML, like Gecko) CriOS/136.0.11304.219 Mobile/15E148 Safari/605.1.98
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/30.0 Chrome/143.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/152.3 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 12_4_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/12.4.3 Mobile/16G130 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7.5 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.2.3 Mobile/17B111 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 13_1_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/89.0.4389.72 Mobile/17A854 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 18_2_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2.1 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/143.0.7499.151 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 16; SM-G990U2 Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_16 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6.2 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/425.6.927981711 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 16; SM-A176U Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/149.0.7827.137 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 16; TMRV08P5G Build/BQ2A.250925.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 26_2_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/143.0.7499.151 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 16; SM-A546U Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/425.6.927981711 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/147.0.7727.99 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/410.0.875971614 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 12; moto g pure Build/S3RHS32.20-42-10-4-2-15-2-4; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/148.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 16; SM-G990U2 Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/430.3.945886556 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5.2 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/429.1.942703598 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 6.0; Nexus 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1 Ddg/26.5
Mozilla/5.0 (iPad; CPU OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/150.0.7871.51 Mobile/15E148 Safari/604.1
Mozilla/5.0 (iPad; CPU OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/413.1.887139264 Mobile/15E148 Safari/604.1
Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36
Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/150.0.7871.51 Mobile/15E148 Safari/604.1
"""


# De-duplicate against everything already in the pool (some new lines repeat an
# existing UA) and within the new batch itself.
_seen_uas = {d["user_agent"] for d in DEVICE_POOL}
for _blk in (_EXTRA_UA_BLOCK, _EXTRA_UA_BLOCK_2):
    for _line in _blk.strip().splitlines():
        _dev = _ua_to_device(_line)
        if _dev and _dev["user_agent"] not in _seen_uas:
            DEVICE_POOL.append(_dev)
            _seen_uas.add(_dev["user_agent"])
