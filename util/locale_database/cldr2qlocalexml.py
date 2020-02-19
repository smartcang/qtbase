#!/usr/bin/env python2
# coding=utf8
#############################################################################
##
## Copyright (C) 2018 The Qt Company Ltd.
## Contact: https://www.qt.io/licensing/
##
## This file is part of the test suite of the Qt Toolkit.
##
## $QT_BEGIN_LICENSE:GPL-EXCEPT$
## Commercial License Usage
## Licensees holding valid commercial Qt licenses may use this file in
## accordance with the commercial license agreement provided with the
## Software or, alternatively, in accordance with the terms contained in
## a written agreement between you and The Qt Company. For licensing terms
## and conditions see https://www.qt.io/terms-conditions. For further
## information use the contact form at https://www.qt.io/contact-us.
##
## GNU General Public License Usage
## Alternatively, this file may be used under the terms of the GNU
## General Public License version 3 as published by the Free Software
## Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
## included in the packaging of this file. Please review the following
## information to ensure the GNU General Public License requirements will
## be met: https://www.gnu.org/licenses/gpl-3.0.html.
##
## $QT_END_LICENSE$
##
#############################################################################
"""Convert CLDR data to qLocaleXML

The CLDR data can be downloaded from CLDR_, which has a sub-directory
for each version; you need the ``core.zip`` file for your version of
choice (typically the latest).  This script has had updates to cope up
to v35; for later versions, we may need adaptations.  Unpack the
downloaded ``core.zip`` and check it has a common/main/ sub-directory:
pass the path of that sub-directory to this script as its single
command-line argument.  Save its standard output (but not error) to a
file for later processing by ``./qlocalexml2cpp.py``

When you update the CLDR data, be sure to also update
src/corelib/text/qt_attribution.json's entry for unicode-cldr.  Check
this script's output for unknown language, country or script messages;
if any can be resolved, use their entry in common/main/en.xml to
append new entries to enumdata.py's lists and update documentation in
src/corelib/text/qlocale.qdoc, adding the new entries in alphabetic
order.

While updating the locale data, check also for updates to MS-Win's
time zone names; see cldr2qtimezone.py for details.

.. _CLDR: ftp://unicode.org/Public/cldr/
"""

import os
import sys
import re
import textwrap

import enumdata
from localetools import Error
from xpathlite import DraftResolution, findAlias, findEntry, findTagsInFile, codeMapsFromFile, \
    _findEntryInFile as findEntryInFile
from dateconverter import convert_date
from qlocalexml import Locale, QLocaleXmlWriter

# TODO: make calendars a command-line option
calendars = ['gregorian', 'persian', 'islamic'] # 'hebrew'
def wrappedwarn(err, prefix, tokens):
    return err.write(
        '\n'.join(textwrap.wrap(prefix + ', '.join(tokens),
                                subsequent_indent=' ', width=80)) + '\n')

def parse_number_format(patterns, data):
    # this is a very limited parsing of the number format for currency only.
    def skip_repeating_pattern(x):
        p = x.replace('0', '#').replace(',', '').replace('.', '')
        seen = False
        result = ''
        for c in p:
            if c == '#':
                if seen:
                    continue
                seen = True
            else:
                seen = False
            result = result + c
        return result
    patterns = patterns.split(';')
    result = []
    for pattern in patterns:
        pattern = skip_repeating_pattern(pattern)
        pattern = pattern.replace('#', "%1")
        # according to http://www.unicode.org/reports/tr35/#Number_Format_Patterns
        # there can be doubled or trippled currency sign, however none of the
        # locales use that.
        pattern = pattern.replace(u'\xa4', "%2")
        pattern = pattern.replace("''", "###").replace("'", '').replace("###", "'")
        pattern = pattern.replace('-', data['minus'])
        pattern = pattern.replace('+', data['plus'])
        result.append(pattern)
    return result

cldr_dir = None
def raiseUnknownCode(code, form, cache={}):
    """Check whether an unknown code could be supported.

    We declare a language, script or country code unknown if it's not
    known to enumdata.py; however, if it's present in main/en.xml's
    mapping of codes to names, we have the option of adding support.
    This caches the necessary look-up (so we only read main/en.xml
    once) and returns the name we should use if we do add support.

    First parameter, code, is the unknown code.  Second parameter,
    form, is one of 'language', 'script' or 'country' to select the
    type of code to look up.  Do not pass further parameters (the next
    will deprive you of the cache).

    Raises localetools.Error with a suitable message, that includes
    the unknown code's full name if found.

    Relies on global cldr_dir being set before it's called; see tail
    of this file.
    """
    if not cache:
        cache.update(codeMapsFromFile(os.path.join(cldr_dir, 'en.xml')))
    name = cache[form].get(code)
    msg = 'unknown %s code "%s"' % (form, code)
    if name:
        msg += ' - could use "%s"' % name
    raise Error(msg)

def parse_list_pattern_part_format(pattern):
    # This is a very limited parsing of the format for list pattern part only.
    return pattern.replace("{0}", "%1").replace("{1}", "%2").replace("{2}", "%3")

def unit_quantifiers(find, path, stem, suffix, known,
                     # Stop at exa/exbi: 16 exbi = 2^{64} < zetta =
                     # 1000^7 < zebi = 2^{70}, the next quantifiers up:
                     si_quantifiers = ('kilo', 'mega', 'giga', 'tera', 'peta', 'exa')):
    """Work out the unit quantifiers.

    Unfortunately, the CLDR data only go up to terabytes and we want
    all the way to exabytes; but we can recognize the SI quantifiers
    as prefixes, strip and identify the tail as the localized
    translation for 'B' (e.g. French has 'octet' for 'byte' and uses
    ko, Mo, Go, To from which we can extrapolate Po, Eo).

    Should be called first for the SI quantifiers, with suffix = 'B',
    then for the IEC ones, with suffix = 'iB'; the list known
    (initially empty before first call) is used to let the second call
    know what the first learned about the localized unit.
    """
    if suffix == 'B': # first call, known = []
        tail = suffix
        for q in si_quantifiers:
            it = find(path, stem % q)
            # kB for kilobyte, in contrast with KiB for IEC:
            q = q[0] if q == 'kilo' else q[0].upper()
            if not it:
                it = q + tail
            elif it.startswith(q):
                rest = it[1:]
                tail = rest if all(rest == k for k in known) else suffix
                known.append(rest)
            yield it
    else: # second call, re-using first's known
        assert suffix == 'iB'
        if known:
            byte = known.pop()
            if all(byte == k for k in known):
                suffix = 'i' + byte
        for q in si_quantifiers:
            yield find(path, stem % q[:2],
                       # Those don't (yet, v31) exist in CLDR, so we always fall back to:
                       q[0].upper() + suffix)

def generateLocaleInfo(path):
    if not path.endswith(".xml"):
        return {}

    # skip legacy/compatibility ones
    alias = findAlias(path)
    if alias:
        raise Error('Alias to "{}"'.format(alias))

    def code(tag):
        return findEntryInFile(path, 'identity/' + tag, attribute="type")[0]

    return _generateLocaleInfo(path, code('language'), code('script'),
                               code('territory'), code('variant'))

def getNumberSystems(cache={}):
    """Cached look-up of number system information.

    Pass no arguments.  Returns a mapping from number system names to,
    for each system, a mapping with keys 'digits', 'type' and 'id'.
    Relies on global cldr_dir being set before it's first called.\n"""
    if not cache:
        for ns in findTagsInFile(os.path.join(cldr_dir, '..', 'supplemental',
                                              'numberingSystems.xml'),
                                 'numberingSystems'):
            # ns has form: [u'numberingSystem', [(u'digits', u'0123456789'), (u'type', u'numeric'), (u'id', u'latn')]]
            entry = dict(ns[1])
            name = entry[u'id']
            if u'digits' in entry and ord(entry[u'digits'][0]) > 0xffff:
                # FIXME, QTBUG-69324: make this redundant:
                # omit number system if zero doesn't fit in single-char16 UTF-16 :-(
                sys.stderr.write('skipping number system "%s" [can\'t represent its zero, U+%X]\n'
                                 % (name, ord(entry[u'digits'][0])))
            else:
                cache[name] = entry
    return cache

def _generateLocaleInfo(path, language_code, script_code, country_code, variant_code=""):
    if not path.endswith(".xml"):
        return {}

    if language_code == 'root':
        # just skip it
        return {}

    # we do not support variants
    # ### actually there is only one locale with variant: en_US_POSIX
    #     does anybody care about it at all?
    if variant_code:
        raise Error('We do not support variants ("{}")'.format(variant_code))

    language_id = enumdata.languageCodeToId(language_code)
    if language_id <= 0:
        raiseUnknownCode(language_code, 'language')

    script_id = enumdata.scriptCodeToId(script_code)
    if script_id == -1:
        raiseUnknownCode(script_code, 'script')

    # we should handle fully qualified names with the territory
    if not country_code:
        return {}
    country_id = enumdata.countryCodeToId(country_code)
    if country_id <= 0:
        raiseUnknownCode(country_code, 'country')

    # So we say we accept only those values that have "contributed" or
    # "approved" resolution. see http://www.unicode.org/cldr/process.html
    # But we only respect the resolution for new datas for backward
    # compatibility.
    draft = DraftResolution.contributed

    result = dict(
        language=enumdata.language_list[language_id][0],
        language_code=language_code, language_id=language_id,
        script=enumdata.script_list[script_id][0],
        script_code=script_code, script_id=script_id,
        country=enumdata.country_list[country_id][0],
        country_code=country_code, country_id=country_id,
        variant_code=variant_code)

    (dir_name, file_name) = os.path.split(path)
    def from_supplement(tag,
                        path=os.path.join(dir_name, '..', 'supplemental',
                                          'supplementalData.xml')):
        return findTagsInFile(path, tag)
    currencies = from_supplement('currencyData/region[iso3166=%s]' % country_code)
    result['currencyIsoCode'] = ''
    result['currencyDigits'] = 2
    result['currencyRounding'] = 1
    if currencies:
        for e in currencies:
            if e[0] == 'currency':
                t = [x[1] == 'false' for x in e[1] if x[0] == 'tender']
                if t and t[0]:
                    pass
                elif not any(x[0] == 'to' for x in e[1]):
                    result['currencyIsoCode'] = (x[1] for x in e[1] if x[0] == 'iso4217').next()
                    break
        if result['currencyIsoCode']:
            t = from_supplement("currencyData/fractions/info[iso4217=%s]"
                                % result['currencyIsoCode'])
            if t and t[0][0] == 'info':
                result['currencyDigits'] = (int(x[1]) for x in t[0][1] if x[0] == 'digits').next()
                result['currencyRounding'] = (int(x[1]) for x in t[0][1] if x[0] == 'rounding').next()
    numbering_system = None
    try:
        numbering_system = findEntry(path, "numbers/defaultNumberingSystem")
    except Error:
        pass
    def findEntryDef(path, xpath, value=''):
        try:
            return findEntry(path, xpath)
        except Error:
            return value
    def get_number_in_system(path, xpath, numbering_system):
        if numbering_system:
            try:
                return findEntry(path, xpath + "[numberSystem=" + numbering_system + "]")
            except Error:
                # in CLDR 1.9 number system was refactored for numbers (but not for currency)
                # so if previous findEntry doesn't work we should try this:
                try:
                    return findEntry(path, xpath.replace("/symbols/", "/symbols[numberSystem=" + numbering_system + "]/"))
                except Error:
                    # fallback to default
                    pass
        return findEntry(path, xpath)

    result['decimal'] = get_number_in_system(path, "numbers/symbols/decimal", numbering_system)
    result['group'] = get_number_in_system(path, "numbers/symbols/group", numbering_system)
    assert result['decimal'] != result['group']
    result['list'] = get_number_in_system(path, "numbers/symbols/list", numbering_system)
    result['percent'] = get_number_in_system(path, "numbers/symbols/percentSign", numbering_system)
    try:
        result['zero'] = getNumberSystems()[numbering_system][u"digits"][0]
    except Exception as e:
        sys.stderr.write("Native zero detection problem: %s\n" % repr(e))
        result['zero'] = get_number_in_system(path, "numbers/symbols/nativeZeroDigit", numbering_system)
    result['minus'] = get_number_in_system(path, "numbers/symbols/minusSign", numbering_system)
    result['plus'] = get_number_in_system(path, "numbers/symbols/plusSign", numbering_system)
    result['exp'] = get_number_in_system(path, "numbers/symbols/exponential", numbering_system).lower()
    result['quotationStart'] = findEntry(path, "delimiters/quotationStart")
    result['quotationEnd'] = findEntry(path, "delimiters/quotationEnd")
    result['alternateQuotationStart'] = findEntry(path, "delimiters/alternateQuotationStart")
    result['alternateQuotationEnd'] = findEntry(path, "delimiters/alternateQuotationEnd")
    result['listPatternPartStart'] = parse_list_pattern_part_format(findEntry(path, "listPatterns/listPattern/listPatternPart[start]"))
    result['listPatternPartMiddle'] = parse_list_pattern_part_format(findEntry(path, "listPatterns/listPattern/listPatternPart[middle]"))
    result['listPatternPartEnd'] = parse_list_pattern_part_format(findEntry(path, "listPatterns/listPattern/listPatternPart[end]"))
    result['listPatternPartTwo'] = parse_list_pattern_part_format(findEntry(path, "listPatterns/listPattern/listPatternPart[2]"))
    result['am'] = findEntry(path, "dates/calendars/calendar[gregorian]/dayPeriods/dayPeriodContext[format]/dayPeriodWidth[wide]/dayPeriod[am]", draft)
    result['pm'] = findEntry(path, "dates/calendars/calendar[gregorian]/dayPeriods/dayPeriodContext[format]/dayPeriodWidth[wide]/dayPeriod[pm]", draft)
    result['longDateFormat'] = convert_date(findEntry(path, "dates/calendars/calendar[gregorian]/dateFormats/dateFormatLength[full]/dateFormat/pattern"))
    result['shortDateFormat'] = convert_date(findEntry(path, "dates/calendars/calendar[gregorian]/dateFormats/dateFormatLength[short]/dateFormat/pattern"))
    result['longTimeFormat'] = convert_date(findEntry(path, "dates/calendars/calendar[gregorian]/timeFormats/timeFormatLength[full]/timeFormat/pattern"))
    result['shortTimeFormat'] = convert_date(findEntry(path, "dates/calendars/calendar[gregorian]/timeFormats/timeFormatLength[short]/timeFormat/pattern"))

    endonym = None
    if country_code and script_code:
        endonym = findEntryDef(path, "localeDisplayNames/languages/language[type=%s_%s_%s]" % (language_code, script_code, country_code))
    if not endonym and script_code:
        endonym = findEntryDef(path, "localeDisplayNames/languages/language[type=%s_%s]" % (language_code, script_code))
    if not endonym and country_code:
        endonym = findEntryDef(path, "localeDisplayNames/languages/language[type=%s_%s]" % (language_code, country_code))
    if not endonym:
        endonym = findEntryDef(path, "localeDisplayNames/languages/language[type=%s]" % (language_code))
    result['languageEndonym'] = endonym
    result['countryEndonym'] = findEntryDef(path, "localeDisplayNames/territories/territory[type=%s]" % (country_code))

    currency_format = get_number_in_system(path, "numbers/currencyFormats/currencyFormatLength/currencyFormat/pattern", numbering_system)
    currency_format = parse_number_format(currency_format, result)
    result['currencyFormat'] = currency_format[0]
    result['currencyNegativeFormat'] = ''
    if len(currency_format) > 1:
        result['currencyNegativeFormat'] = currency_format[1]

    result['currencySymbol'] = ''
    result['currencyDisplayName'] = ''
    if result['currencyIsoCode']:
        result['currencySymbol'] = findEntryDef(path, "numbers/currencies/currency[%s]/symbol" % result['currencyIsoCode'])
        result['currencyDisplayName'] = ';'.join(
            findEntryDef(path, 'numbers/currencies/currency[' + result['currencyIsoCode']
                         + ']/displayName' + tail)
            for tail in ['',] + [
                '[count=%s]' % x for x in ('zero', 'one', 'two', 'few', 'many', 'other')
                ]) + ';'

    def findUnitDef(path, stem, fallback=''):
        # The displayName for a quantified unit in en.xml is kByte
        # instead of kB (etc.), so prefer any unitPattern provided:
        for count in ('many', 'few', 'two', 'other', 'zero', 'one'):
            try:
                ans = findEntry(path, stem + 'unitPattern[count=%s]' % count)
            except Error:
                continue

            # TODO: epxloit count-handling, instead of discarding placeholders
            if ans.startswith('{0}'):
                ans = ans[3:].lstrip()
            if ans:
                return ans

        return findEntryDef(path, stem + 'displayName', fallback)

    # First without quantifier, then quantified each way:
    result['byte_unit'] = findEntryDef(
        path, 'units/unitLength[type=long]/unit[type=digital-byte]/displayName',
        'bytes')
    stem = 'units/unitLength[type=short]/unit[type=digital-%sbyte]/'
    known = [] # cases where we *do* have a given version:
    result['byte_si_quantified'] = ';'.join(unit_quantifiers(findUnitDef, path, stem, 'B', known))
    # IEC 60027-2
    # http://physics.nist.gov/cuu/Units/binary.html
    result['byte_iec_quantified'] = ';'.join(unit_quantifiers(findUnitDef, path, stem % '%sbi', 'iB', known))

    # Used for month and day data:
    namings = (
        ('standaloneLong', 'stand-alone', 'wide'),
        ('standaloneShort', 'stand-alone', 'abbreviated'),
        ('standaloneNarrow', 'stand-alone', 'narrow'),
        ('long', 'format', 'wide'),
        ('short', 'format', 'abbreviated'),
        ('narrow', 'format', 'narrow'),
        )

    # Month names for 12-month calendars:
    for cal in calendars:
        stem = 'dates/calendars/calendar[' + cal + ']/months/'
        for (key, mode, size) in namings:
            prop = 'monthContext[' + mode + ']/monthWidth[' + size + ']/'
            result[key + 'Months_' + cal] = ';'.join(
                findEntry(path, stem + prop + "month[%d]" % i)
                for i in range(1, 13)) + ';'

    # Day data (for Gregorian, at least):
    stem = 'dates/calendars/calendar[gregorian]/days/'
    days = ('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat')
    for (key, mode, size) in namings:
        prop = 'dayContext[' + mode + ']/dayWidth[' + size + ']/day'
        result[key + 'Days'] = ';'.join(
            findEntry(path, stem + prop + '[' + day + ']')
            for day in days) + ';'

    return Locale(result)

def integrateWeekData(filePath, locale_database):
    if not filePath.endswith(".xml"):
        return {}

    def lookup(key):
        return findEntryInFile(filePath, key, attribute='territories')[0].split()
    days = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

    firstDayByCountryCode = {}
    for day in days:
        for countryCode in lookup('weekData/firstDay[day=%s]' % day):
            firstDayByCountryCode[countryCode] = day

    weekendStartByCountryCode = {}
    for day in days:
        for countryCode in lookup('weekData/weekendStart[day=%s]' % day):
            weekendStartByCountryCode[countryCode] = day

    weekendEndByCountryCode = {}
    for day in days:
        for countryCode in lookup('weekData/weekendEnd[day=%s]' % day):
            weekendEndByCountryCode[countryCode] = day

    for (key, locale) in locale_database.iteritems():
        countryCode = locale.country_code
        if countryCode in firstDayByCountryCode:
            locale.firstDayOfWeek = firstDayByCountryCode[countryCode]
        else:
            locale.firstDayOfWeek = firstDayByCountryCode["001"]

        if countryCode in weekendStartByCountryCode:
            locale.weekendStart = weekendStartByCountryCode[countryCode]
        else:
            locale.weekendStart = weekendStartByCountryCode["001"]

        if countryCode in weekendEndByCountryCode:
            locale.weekendEnd = weekendEndByCountryCode[countryCode]
        else:
            locale.weekendEnd = weekendEndByCountryCode["001"]

def splitLocale(name):
    """Split name into (language, script, territory) triple as generator.

    Ignores any trailing fields (with a warning), leaves script (a capitalised
    four-letter token) or territory (either a number or an all-uppercase token)
    empty if unspecified, returns a single-entry generator if name is a single
    tag (i.e. contains no underscores).  Always yields 1 or 3 values, never 2."""
    tags = iter(name.split('_'))
    yield tags.next() # Language
    tag = tags.next()

    # Script is always four letters, always capitalised:
    if len(tag) == 4 and tag[0].isupper() and tag[1:].islower():
        yield tag
        try:
            tag = tags.next()
        except StopIteration:
            tag = ''
    else:
        yield ''

    # Territory is upper-case or numeric:
    if tag and tag.isupper() or tag.isdigit():
        yield tag
        tag = ''
    else:
        yield ''

    # If nothing is left, StopIteration will avoid the warning:
    tag = (tag if tag else tags.next(),)
    sys.stderr.write('Ignoring unparsed cruft %s in %s\n' % ('_'.join(tag + tuple(tags)), name))

def _parseLocale(l):
    language = "AnyLanguage"
    script = "AnyScript"
    country = "AnyCountry"

    if l == "und":
        raise Error('We treat unknown locale like C')

    parsed = splitLocale(l)
    language_code = parsed.next()
    script_code = country_code = ''
    try:
        script_code, country_code = parsed
    except ValueError:
        pass

    if language_code != "und":
        language_id = enumdata.languageCodeToId(language_code)
        if language_id == -1:
            raise Error('Unknown language code "{}"'.format(language_code))
        language = enumdata.language_list[language_id][0]

    if script_code:
        script_id = enumdata.scriptCodeToId(script_code)
        if script_id == -1:
            raise Error('Unknown script code "{}"'.format(script_code))
        script = enumdata.script_list[script_id][0]

    if country_code:
        country_id = enumdata.countryCodeToId(country_code)
        if country_id == -1:
            raise Error('Unknown country code "{}"'.format(country_code))
        country = enumdata.country_list[country_id][0]

    return (language, script, country)

def likelySubtags(root, err):
    skips = []
    for ns in findTagsInFile(os.path.join(root, 'supplemental', 'likelySubtags.xml'), "likelySubtags"):
        tmp = {}
        for data in ns[1:][0]: # ns looks like this: [u'likelySubtag', [(u'from', u'aa'), (u'to', u'aa_Latn_ET')]]
            tmp[data[0]] = data[1]

        try:
            from_language, from_script, from_country = _parseLocale(tmp[u"from"])
            to_language, to_script, to_country = _parseLocale(tmp[u"to"])
        except Error as e:
            if (tmp['to'].startswith(tmp['from'])
                and e.message == 'Unknown language code "{}"'.format(tmp['from'])):
                skips.append(tmp['to'])
            else:
                sys.stderr.write('skipping likelySubtag "{}" -> "{}" ({})\n'.format(
                        tmp[u"from"], tmp[u"to"], e.message))
            continue
        # substitute according to http://www.unicode.org/reports/tr35/#Likely_Subtags
        if to_country == "AnyCountry" and from_country != to_country:
            to_country = from_country
        if to_script == "AnyScript" and from_script != to_script:
            to_script = from_script

        yield ((from_language, from_script, from_country),
               (to_language, to_script, to_country))
    if skips:
        wrappedwarn(err, 'skipping likelySubtags (for unknown language codes): ', skips)

def usage(err, name, message = ''):
    err.write("""Usage: {} <path-to-cldr-main> [out-file.xml]
""".format(name)) # TODO: expand
    if message:
        err.write('\n' + message + '\n')

def main(args, out, err):
    name = args.pop(0)

    if len(args) < 1:
        usage(err, name)
        return 1

    global cldr_dir
    cldr_dir = args.pop(0)
    if not os.path.isdir(cldr_dir):
        usage(err, name, 'Where did you unpack the CLDR data files ?')
        return 1

    if len(args) > 1:
        usage(err, name, 'Too many arguments passed')
        return 1
    if args:
        qxml = open(args.pop(0), 'w')
    else:
        qxml = out

    getNumberSystems(cldr_dir)
    cldr_files = os.listdir(cldr_dir)
    locale_database = {}

    # see http://www.unicode.org/reports/tr35/tr35-info.html#Default_Content
    defaultContent_locales = []
    for ns in findTagsInFile(os.path.join(cldr_dir, '..', 'supplemental',
                                          'supplementalMetadata.xml'),
                             'metadata/defaultContent'):
        for data in ns[1:][0]:
            if data[0] == u"locales":
                defaultContent_locales += data[1].split()

    skips = []
    for file in defaultContent_locales:
        try:
            language_code, script_code, country_code = splitLocale(file)
        except ValueError:
            sys.stderr.write('skipping defaultContent locale "' + file + '" [neither two nor three tags]\n')
            continue

        if not (script_code or country_code):
            sys.stderr.write('skipping defaultContent locale "' + file + '" [second tag is neither script nor territory]\n')
            continue

        try:
            l = _generateLocaleInfo(cldr_dir + "/" + file + ".xml", language_code, script_code, country_code)
            if not l:
                skips.append(file)
                continue
        except Error as e:
            sys.stderr.write('skipping defaultContent locale "{}" ({})\n'.format(file, e.message))
            continue

        locale_database[(l.language_id, l.script_id, l.country_id, l.variant_code)] = l

    if skips:
        wrappedwarn(err, 'skipping defaultContent locales [no locale info generated]: ', skips)
        skips = []

    for file in cldr_files:
        try:
            l = generateLocaleInfo(cldr_dir + "/" + file)
            if not l:
                skips.append(file)
                continue
        except Error as e:
            sys.stderr.write('skipping file "{}" ({})\n'.format(file, e.message))
            continue

        locale_database[(l.language_id, l.script_id, l.country_id, l.variant_code)] = l

    if skips:
        wrappedwarn(err, 'skipping files [no locale info generated]: ', skips)

    integrateWeekData(cldr_dir + "/../supplemental/supplementalData.xml", locale_database)
    cldr_version = 'unknown'
    with open(cldr_dir+"/../dtd/ldml.dtd", "r") as ldml:
        for line in ldml:
            if 'version cldrVersion CDATA #FIXED' in line:
                cldr_version = line.split('"')[1]

    xmlOut = QLocaleXmlWriter(qxml.write)
    xmlOut.version(cldr_version)
    xmlOut.enumData(enumdata.language_list,
                    enumdata.script_list,
                    enumdata.country_list)
    xmlOut.likelySubTags(likelySubtags(os.path.split(cldr_dir)[0], err))
    xmlOut.locales(locale_database, calendars)
    xmlOut.close()
    if qxml is not out:
        qxml.close()

    return 0

if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv, sys.stdout, sys.stderr))
