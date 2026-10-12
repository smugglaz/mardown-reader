"""Local suggestions from the same Enchant dictionaries used by WebKit."""
import ctypes as C
from ctypes.util import find_library

class SpellingUnavailable(RuntimeError):
    pass


def _lookup(words, *, suggestions=False, language='en_US'):
    words = [word for word in words if isinstance(word, str) and word and len(word) <= 100 and all(c.isalpha() or c in "'’" for c in word)]
    if not words:
        return []
    library = find_library('enchant-2')
    if not library:
        raise SpellingUnavailable('Spelling is unavailable. Install libenchant-2-2 and hunspell-en-us.')
    lib = C.CDLL(library)
    ptr = C.c_void_p
    strings = C.POINTER(C.c_char_p)
    declarations = {
        'enchant_broker_init': ([], ptr),
        'enchant_broker_free': ([ptr], None),
        'enchant_broker_request_dict': ([ptr, C.c_char_p], ptr),
        'enchant_broker_free_dict': ([ptr, ptr], None),
        'enchant_dict_check': ([ptr, C.c_char_p, C.c_ssize_t], C.c_int),
        'enchant_dict_suggest': ([ptr, C.c_char_p, C.c_ssize_t, C.POINTER(C.c_size_t)], strings),
        'enchant_dict_free_string_list': ([ptr, strings], None),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    broker = lib.enchant_broker_init()
    if not broker:
        raise SpellingUnavailable('The local spelling dictionary could not start.')
    dictionary = None
    try:
        dictionary = lib.enchant_broker_request_dict(broker, language.encode('ascii'))
        if not dictionary:
            raise SpellingUnavailable('The English spelling dictionary is unavailable. Install hunspell-en-us.')
        if not suggestions:
            return [word for word in words if lib.enchant_dict_check(dictionary, word.encode('utf-8'), len(word.encode('utf-8'))) == 1]
        source = words[0].encode('utf-8')
        if lib.enchant_dict_check(dictionary, source, len(source)) != 1:
            return []
        count = C.c_size_t()
        values = lib.enchant_dict_suggest(dictionary, source, len(source), C.byref(count))
        if not values:
            return []
        try:
            return list(dict.fromkeys(values[index].decode('utf-8') for index in range(min(count.value, 6))))
        finally:
            lib.enchant_dict_free_string_list(dictionary, values)
    finally:
        if dictionary:
            lib.enchant_broker_free_dict(broker, dictionary)
        lib.enchant_broker_free(broker)


def suggest(word):
    try:
        return _lookup([word], suggestions=True)
    except SpellingUnavailable:
        return []


def misspelled_words(words):
    if not isinstance(words, list) or len(words) > 10000:
        raise ValueError('Spelling request exceeds the 10,000-word batch limit.')
    return _lookup(words)
