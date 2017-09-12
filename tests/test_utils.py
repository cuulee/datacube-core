"""
Test date sequence generation functions as used by statistics apps


"""
import os
import string

import pytest
from dateutil.parser import parse
from hypothesis import given
from hypothesis.strategies import integers, text
from pandas import to_datetime

from datacube.utils import uri_to_local_path, clamp, gen_password, write_user_secret_file, slurp
from datacube.utils.changes import check_doc_unchanged, get_doc_changes, MISSING, DocumentMismatchError
from datacube.utils.dates import date_sequence


def test_stats_dates():
    # Winter for 1990
    winter_1990 = list(date_sequence(start=to_datetime('1990-06-01'), end=to_datetime('1990-09-01'), step_size='3m',
                                     stats_duration='3m'))
    assert winter_1990 == [(parse('1990-06-01'), parse('1990-09-01'))]

    # Every winter from 1990 - 1992
    three_years_of_winter = list(date_sequence(start=to_datetime('1990-06-01'), end=to_datetime('1992-09-01'),
                                               step_size='1y',
                                               stats_duration='3m'))
    assert three_years_of_winter == [(parse('1990-06-01'), parse('1990-09-01')),
                                     (parse('1991-06-01'), parse('1991-09-01')),
                                     (parse('1992-06-01'), parse('1992-09-01'))]

    # Full years from 1990 - 1994
    five_full_years = list(date_sequence(start=to_datetime('1990-01-01'), end=to_datetime('1995'), step_size='1y',
                                         stats_duration='1y'))
    assert five_full_years == [(parse('1990-01-01'), parse('1991-01-01')),
                               (parse('1991-01-01'), parse('1992-01-01')),
                               (parse('1992-01-01'), parse('1993-01-01')),
                               (parse('1993-01-01'), parse('1994-01-01')),
                               (parse('1994-01-01'), parse('1995-01-01'))]

    # Every season (three months), starting in March, from 1990 until end 1992-02
    two_years_of_seasons = list(date_sequence(start=to_datetime('1990-03-01'), end=to_datetime('1992-03'),
                                              step_size='3m',
                                              stats_duration='3m'))
    assert len(two_years_of_seasons) == 8
    assert two_years_of_seasons == [(parse('1990-03-01'), parse('1990-06-01')),
                                    (parse('1990-06-01'), parse('1990-09-01')),
                                    (parse('1990-09-01'), parse('1990-12-01')),
                                    (parse('1990-12-01'), parse('1991-03-01')),
                                    (parse('1991-03-01'), parse('1991-06-01')),
                                    (parse('1991-06-01'), parse('1991-09-01')),
                                    (parse('1991-09-01'), parse('1991-12-01')),
                                    (parse('1991-12-01'), parse('1992-03-01'))]  # Leap year!

    # Every month from 1990-01 to 1990-06
    monthly = list(date_sequence(start=to_datetime('1990-01-01'), end=to_datetime('1990-07-01'), step_size='1m',
                                 stats_duration='1m'))
    assert len(monthly) == 6

    # Complex
    # I want the average over 5 years


def test_uri_to_local_path():
    if os.name == 'nt':
        assert 'C:\\tmp\\test.tmp' == str(uri_to_local_path('file:///C:/tmp/test.tmp'))

    else:
        assert '/tmp/something.txt' == str(uri_to_local_path('file:///tmp/something.txt'))

    assert uri_to_local_path(None) is None

    with pytest.raises(ValueError):
        uri_to_local_path('ftp://example.com/tmp/something.txt')


@given(integers(), integers(), integers())
def test_clamp(x, lower_bound, upper_bound):
    if lower_bound > upper_bound:
        lower_bound, upper_bound = upper_bound, lower_bound
    new_x = clamp(x, lower_bound, upper_bound)

    # If x was already between the bounds, it shouldn't have changed
    if lower_bound <= x <= upper_bound:
        assert new_x == x
    assert lower_bound <= new_x <= upper_bound


@given(integers(min_value=10, max_value=30))
def test_gen_pass(n_bytes):
    password1 = gen_password(n_bytes)
    password2 = gen_password(n_bytes)
    assert len(password1) >= n_bytes
    assert len(password2) >= n_bytes
    assert password1 != password2


@given(text(alphabet=string.digits + string.ascii_letters + ' ,:.![]?', max_size=20))
def test_write_user_secret_file(txt):
    fname = u".tst-datacube-uefvwr4cfkkl0ijk.txt"

    write_user_secret_file(txt, fname)
    txt_back = slurp(fname)
    os.remove(fname)
    assert txt == txt_back
    assert slurp(fname) is None


doc_changes = [
    (1, 1, []),
    ({}, {}, []),
    ({'a': 1}, {'a': 1}, []),
    ({'a': {'b': 1}}, {'a': {'b': 1}}, []),
    ([1, 2, 3], [1, 2, 3], []),
    ([1, 2, [3, 4, 5]], [1, 2, [3, 4, 5]], []),
    (1, 2, [((), 1, 2)]),
    ([1, 2, 3], [2, 1, 3], [((0,), 1, 2), ((1,), 2, 1)]),
    ([1, 2, [3, 4, 5]], [1, 2, [3, 6, 7]], [((2, 1), 4, 6), ((2, 2), 5, 7)]),
    ({'a': 1}, {'a': 2}, [(('a',), 1, 2)]),
    ({'a': 1}, {'a': 2}, [(('a',), 1, 2)]),
    ({'a': 1}, {'b': 1}, [(('a',), 1, MISSING), (('b',), MISSING, 1)]),
    ({'a': {'b': 1}}, {'a': {'b': 2}}, [(('a', 'b'), 1, 2)]),
    ({}, {'b': 1}, [(('b',), MISSING, 1)]),
    ({'a': {'c': 1}}, {'a': {'b': 1}}, [(('a', 'b'), MISSING, 1), (('a', 'c'), 1, MISSING)])
]


@pytest.mark.parametrize("v1, v2, expected", doc_changes)
def test_get_doc_changes(v1, v2, expected):
    rval = get_doc_changes(v1, v2)
    assert rval == expected


def test_get_doc_changes():
    rval = get_doc_changes({}, None, base_prefix=('a',))
    assert rval == [(('a',), {}, None)]


@pytest.mark.parametrize("v1, v2, expected", doc_changes)
def test_check_doc_unchanged(v1, v2, expected):
    if expected != []:
        with pytest.raises(DocumentMismatchError):
            rval = check_doc_unchanged(v1, v2, 'name')
    else:
        # No Error Raised
        check_doc_unchanged(v1, v2, 'name')


def test_more_check_doc_unchanged():
    # No exception raised
    check_doc_unchanged({'a': 1}, {'a': 1}, 'Letters')

    with pytest.raises(DocumentMismatchError, message='Letters differs from stored (a: 1!=2)'):
        check_doc_unchanged({'a': 1}, {'a': 2}, 'Letters')

    with pytest.raises(DocumentMismatchError, message='Letters differs from stored (a.b: 1!=2)'):
        check_doc_unchanged({'a': {'b': 1}}, {'a': {'b': 2}}, 'Letters')
