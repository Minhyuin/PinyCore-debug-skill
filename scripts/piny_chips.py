"""What each supported chip is, so a profile only has to name the silicon it drives.

Every fact here is a property of the SILICON, not of one computer and not of one built image,
which is exactly why it belongs in reviewed code rather than in a profile somebody copies by
hand. Before this module existed those facts lived as module constants in :mod:`piny_debug`
(``EXPECTED_DEVICE_ID = 0x483``, ``EXPECTED_CPUID = 0x411fc272``, ``RCC_RSR``), so the second
board on the same bench was rejected by the first board's identity check.

The split it implements is the one :func:`piny_debug.portability_report` already documents:

* **machine** keys (a tool path, a checkout path) differ per computer and discovery can relocate
  them;
* **target** keys differ per BOARD and must be re-established rather than copied;
* **chip** keys -- the ones in here -- differ per part number, are the same for every board that
  carries that part, and can therefore be shipped, reviewed and tested.

A profile states ``"chip": "stm32f407ighx"`` and gets the rest. Anything the profile states
explicitly still wins, so a rig that genuinely differs (an overclocked board, a part driven by a
different debug transport) can say so and be believed.

Two of these values are worth spelling out because they are the ones that silently corrupt
measurements rather than failing loudly:

* ``cycle_frequency_hz`` is the clock SystemView divides its timestamps by. The value here is the
  one this project's firmware configures; ``identify`` reads the same number back off the live
  target's ``SystemCoreClock`` so a mismatch is reported instead of quietly scaling every CPU
  load.
* ``reset_cause_address`` matters because the reset-cause register is *read-to-clear*: reading
  it at the wrong address on the wrong family returns some other peripheral's bits and destroys
  nothing, but reading it at the right address in the wrong order destroys the evidence.
"""

#: ARM's implementer code for bits 31:24 of CPUID. 0x41 is ARM Limited.
ARM_IMPLEMENTER = 0x41

#: CPUID bits 15:4 -> the core that reports them. Vendors use these part numbers verbatim, so
#: this is how a core is named without a table per chip.
_CORE_PARTS = {
    0xc20: 'Cortex-M0',
    0xc21: 'Cortex-M1',
    0xc23: 'Cortex-M3',
    0xc24: 'Cortex-M4',
    0xc27: 'Cortex-M7',
    0xc60: 'Cortex-M0+',
    0xc61: 'Cortex-M0+',
    0xc64: 'Cortex-M4',
    0xd20: 'Cortex-M23',
    0xd21: 'Cortex-M33',
    0xd22: 'Cortex-M55',
    0xd23: 'Cortex-M85',
}

#: The reset-cause bits of the STM32H7 RCC (RCC_RSR, RCC base 0x58024C00 + 0x0D0), from
#: ``stm32h723xx.h``. Reading this register clears it.
_H7_RESET_FLAGS = ((16, 'PINRST'), (17, 'BORRST'), (18, 'SFTRST'), (19, 'IWDG1RST'),
                   (20, 'WWDG1RST'), (21, 'LPWRRST'), (22, 'D1RST'), (23, 'D2RST'),
                   (24, 'CPURST'), (25, 'D1D2RST'))

#: The reset-cause bits of the STM32F4 RCC (RCC_CSR, RCC base 0x40023800 + 0x74), from
#: ``stm32f407xx.h``. Same register role as the H7's, different address and different names,
#: which is the whole reason this cannot be a module constant. Bit 24 (RMVF) is the
#: clear-flag control, not a cause, so it is deliberately not listed as one.
_F4_RESET_FLAGS = ((31, 'LPWRRST'), (30, 'WWDGRST'), (29, 'IWDGRST'), (28, 'SFTRST'),
                   (27, 'PORRST'), (26, 'PINRST'), (25, 'BORRST'))

#: One entry per supported part number. Keys are the ``chip`` value a profile writes.
#: ``jlink_device`` is SEGGER's own device name, which encodes the package and the flash size:
#: flashing the wrong one is not a syntax error, it is a wrong target, so it is stated here
#: rather than guessed from the chip key.
CHIPS = {
    'stm32h723vgtx': {
        'chip': 'STM32H723VGTx',
        'core': 'Cortex-M7',
        'jlink_device': 'STM32H723VG',
        'expected_cpuid': 0x411fc272,
        'expected_device_id': 0x483,
        'idcode_address': 0x5c001000,
        'reset_cause_address': 0x58024c00 + 0x0d0,
        'reset_cause_flags': _H7_RESET_FLAGS,
        # DTCM 128K, AXI SRAM 128K, D2 SRAM 32K, D3 SRAM 16K -- the memory SystemView scans for
        # the RTT control block, from Src/Common/Soc/stm32h723vgtx/link.ld.
        'ram_ranges': ((0x20000000, 131072), (0x24000000, 131072),
                       (0x30000000, 32768), (0x38000000, 16384)),
        'cycle_frequency_hz': 550000000,
        'source': 'Src/Common/Soc/stm32h723vgtx/{link.ld,stm32h723xx.h}',
    },
    'stm32f407ighx': {
        'chip': 'STM32F407IGHx',
        'core': 'Cortex-M4',
        'jlink_device': 'STM32F407IG',
        'expected_cpuid': 0x410fc241,
        'expected_device_id': 0x413,
        'idcode_address': 0xe0042000,
        'reset_cause_address': 0x40023800 + 0x74,
        'reset_cause_flags': _F4_RESET_FLAGS,
        # SRAM1 128K at 0x20000000 and CCM 64K at 0x10000000, from
        # Src/Common/Soc/stm32f407ighx/link.ld. The part has 192K of RAM in two blocks the
        # linker keeps separate, so both must be listed for the RTT control block to be found
        # wherever the build put it.
        'ram_ranges': ((0x20000000, 131072), (0x10000000, 65536)),
        # 12 MHz HSE / PLLM 6 * PLLN 168 / PLLP 2 = 168 MHz, from SystemClock_Config in
        # Src/Common/Soc/stm32f407ighx/hal/Core/Src/main.c (FLASH_LATENCY_5 matches 168 MHz).
        'cycle_frequency_hz': 168000000,
        'source': 'Src/Common/Soc/stm32f407ighx/{link.ld,stm32f407xx.h,hal/Core/Src/main.c}',
    },
}

#: Profile keys the chip table is allowed to fill in. Kept as an explicit list so a typo in an
#: entry cannot silently introduce a profile key that nothing reads.
_TARGET_FACTS = ('chip', 'core', 'jlink_device', 'expected_cpuid', 'expected_device_id',
                 'idcode_address', 'reset_cause_address', 'reset_cause_flags', 'ram_ranges',
                 'cycle_frequency_hz')

#: How each fact is written into a profile. The hex ones are strings because that is what a
#: profile carries (``"0x483"``), the roster-style ones are JSON lists, and the rest are native.
_HEX_FACTS = ('expected_device_id', 'idcode_address', 'reset_cause_address')
_LIST_FACTS = ('ram_ranges', 'reset_cause_flags')


def chip_names():
    """Every part number this package knows, sorted, for messages and tests."""
    return sorted(CHIPS)


def chip_facts(name):
    """The profile-shaped facts for ``name``, or None when the chip is not known.

    An unknown name is not an error here: a profile for silicon this package has never seen is
    a legitimate thing to write, and the caller decides whether to warn or to carry on with the
    profile's own values. Returning None keeps that decision with the caller.
    """
    entry = CHIPS.get(str(name or '').strip().lower())
    if entry is None:
        return None
    facts = {}
    for key in _TARGET_FACTS:
        value = entry[key]
        if key in _HEX_FACTS:
            facts[key] = hex(value)
        elif key in _LIST_FACTS:
            facts[key] = [list(item) for item in value]
        else:
            facts[key] = value
    return facts


def apply_chip(profile):
    """Fill a profile's chip facts in underneath whatever it already states.

    Returns ``(chip_name, filled_keys)``. A profile value always wins -- the point of the table
    is to remove work, not to overrule a rig that was measured -- and the returned key list is
    what lets ``doctor`` say which values it supplied rather than presenting an inferred number
    as if the profile had stated it.

    ``filled_keys`` is empty when the profile names no chip, and also when it names a chip this
    package has never heard of: both cases are "the table contributed nothing", and the caller
    decides what to warn about.
    """
    name = profile.get('chip')
    if not name:
        return None, []
    facts = chip_facts(name)
    if facts is None:
        return str(name).strip().lower(), []
    filled = []
    for key, value in facts.items():
        if key not in profile:
            profile[key] = value
            filled.append(key)
    return str(name).strip().lower(), sorted(filled)


def describe_cpuid(raw):
    """Name the core from a CPUID register read, or say what could not be named.

    ``raw`` is the 32-bit value at 0xE000ED00. The part number is bits 15:4, and the revision is
    two fields that are easy to transpose: bits 23:20 are the variant, which ARM prints as the
    ``rN`` half of ``rNpM`` (1 for an STM32H723's Cortex-M7, 0 for an STM32F407's Cortex-M4),
    while bits 19:16 are the architecture constant -- 0xf for ARMv7-M, which is why reading the
    major revision from there renders every part as r15. Bits 3:0 are the ``pM`` half.
    """
    try:
        raw = int(raw) & 0xffffffff
    except (TypeError, ValueError):
        return {'raw': None, 'core': None, 'note': 'CPUID was not a number'}
    implementer = raw >> 24 & 0xff
    part = raw >> 4 & 0xfff
    return {'raw': hex(raw),
            'implementer': hex(implementer),
            'implementer_name': 'ARM' if implementer == ARM_IMPLEMENTER else None,
            'part': hex(part),
            'core': _CORE_PARTS.get(part),
            'variant': raw >> 20 & 0xf,
            'revision': 'r%d p%d' % (raw >> 20 & 0xf, raw & 0xf)}


def decode_reset_cause(raw, flags):
    """Turn a reset-cause register read into named flags, using the chip's own bit list."""
    try:
        raw = int(raw) & 0xffffffff
    except (TypeError, ValueError):
        return {'raw': None, 'flags': {}}
    return {'raw': hex(raw),
            'flags': {name: bool(raw >> int(shift) & 1) for shift, name in flags}}
