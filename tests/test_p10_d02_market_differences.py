"""Synthetic regression cases for numeric signatures, not semantic certification."""
from decimal import Decimal
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('market_difference_profile', Path(__file__).resolve().parents[1] / 'tools/data/profile_market_differences.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_integer_quantization_does_not_claim_supplier_cause():
    classification, reason = MODULE.classify_amount(Decimal('15876592'), Decimal('15876591.00'))
    assert classification == 'PRECISION_OR_ROUNDING'
    assert 'NOT_PROVEN' in reason


def test_binary32_image_is_not_unit_certification():
    classification, reason = MODULE.classify_amount(Decimal('118538106'), Decimal('118538104'))
    assert classification == 'PRECISION_OR_ROUNDING'
    assert reason == 'EXACT_BINARY32_IMAGE_NOT_PIPELINE_PROOF'


def test_scale_relationship_remains_unresolved():
    assert MODULE.classify_amount(Decimal('12345.67'), Decimal('1234567'))[0] == 'UNRESOLVED'


def test_signed_relative_and_zero_reference_are_not_silently_dropped():
    stat = MODULE.new_stat()
    MODULE.add_stat(stat, Decimal('100'), Decimal('101'), 0, 0)
    MODULE.add_stat(stat, Decimal('0'), Decimal('2'), 1, 1)
    assert stat['positive'] == 0
    assert stat['negative'] == 2
    assert stat['zero_reference'] == 1
    assert stat['rel_max'] == Decimal('.01')
    assert stat['abs_max'] == 2
    assert stat['filled'] == stat['zero_volume'] == 1


def test_scientific_notation_not_confused_with_scale_or_precision():
    assert MODULE.number('1.18538105E8') == Decimal('118538105.00')
    assert MODULE.number('--') is None
    stat = MODULE.new_stat()
    MODULE.add_stat(stat, Decimal('1.18538105E8'), Decimal('118538104.00'), 0, 0)
    assert stat['cs_digits'][9] == 1
    assert stat['ratio_power10'] == 0
