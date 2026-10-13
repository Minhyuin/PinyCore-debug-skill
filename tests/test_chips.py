"""The chip table: the facts that follow from which part number is on the bench.

These cases exist because the failure they prevent is quiet. A wrong CPUID makes a working board
look broken; a wrong clock makes a CPU load look small; a wrong device id makes the flash gate
refuse a board it should accept or, worse, accept one it should refuse. None of them announces
itself, so the table is pinned here against the values that were read off real silicon.
"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import piny_chips as c  # noqa: E402


class CatalogueTests(unittest.TestCase):
    def test_the_known_chips_are_listed(self):
        self.assertEqual(c.chip_names(), ['stm32f407ighx', 'stm32h723vgtx'])

    def test_the_h723_entry_still_matches_the_board_that_verified_it(self):
        """The one entry whose values were read off hardware, pinned as read.

        Every number here was measured on the STM32H723VGTx board this skill was built against:
        the device id and CPUID out of identify, the clock out of SystemCoreClock on the target,
        the RAM ranges out of the linker script. The chip table is a refactor of those facts, so
        if it disagrees with the rig the rig wins and this case is the alarm.
        """
        facts = c.chip_facts('stm32h723vgtx')
        self.assertEqual(facts['chip'], 'STM32H723VGTx')
        self.assertEqual(facts['core'], 'Cortex-M7')
        self.assertEqual(facts['jlink_device'], 'STM32H723VG')
        self.assertEqual(facts['expected_device_id'], '0x483')
        self.assertEqual(facts['idcode_address'], '0x5c001000')
        self.assertEqual(facts['expected_cpuid'], 0x411fc272)
        self.assertEqual(facts['cycle_frequency_hz'], 550000000)
        self.assertEqual(facts['reset_cause_address'], '0x58024cd0')
        self.assertEqual([[int(b), n] for b, n in facts['reset_cause_flags']],
                         [[16, 'PINRST'], [17, 'BORRST'], [18, 'SFTRST'], [19, 'IWDG1RST'],
                          [20, 'WWDG1RST'], [21, 'LPWRRST'], [22, 'D1RST'], [23, 'D2RST'],
                          [24, 'CPURST'], [25, 'D1D2RST']])
        self.assertEqual([[0x20000000, 131072], [0x24000000, 131072], [0x30000000, 32768],
                          [0x38000000, 16384]], facts['ram_ranges'])

    def test_the_f407_entry_encodes_what_the_project_source_says(self):
        """The F407 facts come from the firmware's own headers and linker script.

        DBGMCU_BASE 0xE0042000 and RCC_CSR at 0x40023874 are from stm32f407xx.h; 0x413 is the
        STM32F405/407/415/417 device id; 0x410FC241 is Cortex-M4 r0p1; 168 MHz is 12 MHz HSE
        through PLLM 6 / PLLN 168 / PLLP 2 in SystemClock_Config; the two RAM blocks are SRAM1
        128K at 0x20000000 and CCM 64K at 0x10000000 out of link.ld.
        """
        facts = c.chip_facts('stm32f407ighx')
        self.assertEqual(facts['chip'], 'STM32F407IGHx')
        self.assertEqual(facts['core'], 'Cortex-M4')
        self.assertEqual(facts['jlink_device'], 'STM32F407IG')
        self.assertEqual(facts['expected_device_id'], '0x413')
        self.assertEqual(facts['idcode_address'], '0xe0042000')
        self.assertEqual(facts['expected_cpuid'], 0x410fc241)
        self.assertEqual(facts['cycle_frequency_hz'], 168000000)
        self.assertEqual(facts['reset_cause_address'], '0x40023874')
        self.assertEqual([[0x20000000, 131072], [0x10000000, 65536]], facts['ram_ranges'])

    def test_an_absent_or_unknown_chip_is_not_an_error(self):
        """Unknown silicon is a legitimate rig, so this reports rather than raises."""
        for name in (None, '', 'stm32f103c8tx', '  '):
            self.assertIsNone(c.chip_facts(name), name)

    def test_the_chip_key_is_case_insensitive(self):
        self.assertEqual(c.chip_facts('STM32F407IGHX'), c.chip_facts('stm32f407ighx'))

    def test_every_entry_is_internally_consistent(self):
        """One pass over the table, checking the things that can only be wrong by accident.

        The CPUID check is the interesting one: the core named in the entry has to be the core
        that the expected CPUID actually reports, or the table would name a Cortex-M4 while
        refusing to accept one.
        """
        for name in c.chip_names():
            facts = c.chip_facts(name)
            self.assertEqual(set(facts), set(c._TARGET_FACTS), name)
            self.assertTrue(facts['chip'], name)
            self.assertEqual(facts['core'], c._CORE_PARTS[facts['expected_cpuid'] >> 4 & 0xfff], name)
            for key in c._HEX_FACTS:
                self.assertTrue(facts[key].startswith('0x'), (name, key))
                int(facts[key], 0)
            self.assertGreater(facts['cycle_frequency_hz'], 0, name)
            self.assertTrue(facts['ram_ranges'], name)
            for base, length in facts['ram_ranges']:
                self.assertEqual(base & 0xfff, 0, (name, base))
                self.assertGreater(length, 0, (name, base))
            shifts = [int(bit) for bit, _ in facts['reset_cause_flags']]
            names = [bit_name for _, bit_name in facts['reset_cause_flags']]
            self.assertEqual(len(set(shifts)), len(shifts), name)
            self.assertEqual(len(set(names)), len(names), name)
            for bit in shifts:
                self.assertTrue(0 <= bit <= 31, (name, bit))

    def test_the_two_reset_cause_registers_really_differ(self):
        """The reset-cause register is why this cannot be one module constant.

        Same role on both parts, different address and different bit names, and it is
        read-to-clear -- so reading the wrong one returns another peripheral's bits and the
        evidence is gone.
        """
        h7 = c.chip_facts('stm32h723vgtx'); f4 = c.chip_facts('stm32f407ighx')
        self.assertNotEqual(h7['reset_cause_address'], f4['reset_cause_address'])
        self.assertNotEqual(h7['reset_cause_flags'], f4['reset_cause_flags'])
        self.assertNotEqual(h7['idcode_address'], f4['idcode_address'])


class ApplyTests(unittest.TestCase):
    def test_a_profile_that_states_a_value_keeps_it(self):
        """The table fills gaps; it never overrules a rig somebody measured."""
        profile = {'chip': 'stm32f407ighx', 'cycle_frequency_hz': 100000000,
                   'jlink_device': 'STM32F407VE'}
        name, filled = c.apply_chip(profile)
        self.assertEqual((name, profile['cycle_frequency_hz'], profile['jlink_device']),
                         ('stm32f407ighx', 100000000, 'STM32F407VE'))
        self.assertNotIn('cycle_frequency_hz', filled)
        self.assertNotIn('jlink_device', filled)
        self.assertIn('expected_device_id', filled)
        self.assertEqual(profile['expected_device_id'], '0x413')

    def test_a_profile_with_no_chip_is_left_alone(self):
        profile = {'jlink_device': 'STM32H723VG'}
        self.assertEqual(c.apply_chip(profile), (None, []))
        self.assertEqual(profile, {'jlink_device': 'STM32H723VG'})

    def test_an_unknown_chip_fills_nothing_but_is_still_named(self):
        profile = {'chip': 'stm32f103c8tx'}
        self.assertEqual(c.apply_chip(profile), ('stm32f103c8tx', []))
        self.assertEqual(profile, {'chip': 'stm32f103c8tx'})

    def test_filling_is_complete_enough_to_identify_a_target(self):
        """Whatever the table fills must be enough for the identity check to run."""
        profile = {'chip': 'stm32h723vgtx'}
        c.apply_chip(profile)
        for key in ('idcode_address', 'expected_device_id', 'expected_cpuid'):
            self.assertIn(key, profile)
        self.assertEqual(int(profile['idcode_address'], 0), 0x5c001000)


class DecodeTests(unittest.TestCase):
    def test_the_core_is_named_from_the_cpuid(self):
        """Both values were read off the boards, and both must decode to the right core."""
        m7 = c.describe_cpuid(0x411fc272)
        self.assertEqual(m7['core'], 'Cortex-M7')
        self.assertEqual(m7['implementer_name'], 'ARM')
        self.assertEqual(m7['part'], '0xc27')
        self.assertEqual(m7['revision'], 'r1 p2')
        m4 = c.describe_cpuid(0x410fc241)
        self.assertEqual(m4['core'], 'Cortex-M4')
        self.assertEqual(m4['revision'], 'r0 p1')

    def test_an_unrecognised_core_is_reported_without_a_name(self):
        """A part the table has never seen still has its numbers reported."""
        decoded = c.describe_cpuid(0x410fd999)
        self.assertIsNone(decoded['core'])
        self.assertEqual(decoded['raw'], '0x410fd999')

    def test_a_non_numeric_cpuid_does_not_raise(self):
        self.assertIsNone(c.describe_cpuid(None)['core'])
        self.assertIsNone(c.describe_cpuid('nonsense')['core'])

    def test_reset_flags_are_decoded_with_the_chips_own_bits(self):
        flags = c.chip_facts('stm32f407ighx')['reset_cause_flags']
        decoded = c.decode_reset_cause(1 << 27, flags)
        self.assertEqual([name for name, set_ in decoded['flags'].items() if set_], ['PORRST'])
        h7 = c.chip_facts('stm32h723vgtx')['reset_cause_flags']
        self.assertEqual([name for name, set_ in c.decode_reset_cause(1 << 16, h7)['flags'].items()
                          if set_], ['PINRST'])
        # The bit that means PORRST on the F4 must not be read as a cause on the H7, because a
        # shared list is exactly the mistake that would make the H7 report flags it never had.
        self.assertEqual(c.decode_reset_cause(1 << 27, h7)['flags'].get('PORRST'), None)

    def test_a_non_numeric_reset_register_does_not_raise(self):
        self.assertEqual(c.decode_reset_cause(None, c.chip_facts('stm32f407ighx')['reset_cause_flags']),
                         {'raw': None, 'flags': {}})


if __name__ == '__main__':
    unittest.main()
