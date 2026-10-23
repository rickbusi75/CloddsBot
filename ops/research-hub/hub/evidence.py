"""Lexical matching with explicit subject anchors, not generic market words."""
import re
import unicodedata


STOP = set('''will would should could shall can does did has have had been being
the and for with from into over under above below before after between until
this that these those which what when where who whose how than then there
yes no any all another other first last next new more most less least
market markets price prices reach hit win wins winner become make happen
end year month week day quarter january february march april may june july
august september october november december monday tuesday wednesday thursday
friday saturday sunday president presidential prime minister election elections
senate house governor government candidate party nomination nominee office
le la les un une des du de et ou en au aux sur pour avant apres sera est
'''.split())


def words(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', str(text)) if not unicodedata.combining(c)).casefold()
    return {x for x in re.findall(r'[^\W_]+', text, re.UNICODE)
            if len(x) >= 3 and not x.isdecimal() and x not in STOP}


def relevance(question, title, text=''):
    terms = words(question)
    if not terms:
        return {'score': 0, 'matched_terms': []}
    # Named subjects take precedence: an article about a different president
    # cannot match Putin merely because it discusses an election in October.
    anchors = set()
    for token in re.findall(r'[^\W_]+', str(question), re.UNICODE):
        if any(c.isupper() for c in token):
            anchors.update(words(token))
    title_terms, body_terms = words(title), words(text)
    matches = terms & (title_terms | body_terms)
    if not matches or (anchors and not anchors.intersection(title_terms | body_terms)):
        return {'score': 0, 'matched_terms': []}
    return {'score': len(matches) + 3 * len(terms & title_terms) + 2 * len(anchors & title_terms),
            'matched_terms': sorted(matches)}
