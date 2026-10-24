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

# Explicit lexical variants measured in dossiers; avoid broad stemming that
# could merge unrelated names. Other spellings remain a recall limitation.
ALIASES = {'israeli': 'israel'}

MATCHING = 'subject_anchors_v3_scope'
# A shared party or fragment of a multiword name is not a distinctive subject
# when the question also names a person or location (United Serbia != US).
WEAK_ANCHORS = {'united', 'states', 'democrats', 'democratic', 'republicans', 'republican'}
US_STATES = tuple(tuple(name.split()) for name in '''alabama|alaska|arizona|arkansas|
california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|
indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|
minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|
new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|
rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|
washington|west virginia|wisconsin|wyoming|district of columbia'''.replace('\n', '').split('|'))
ELECTORAL = {'senate', 'house', 'governor', 'gubernatorial', 'congressional',
             'election', 'elections', 'district'}


def tokens(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', str(text)) if not unicodedata.combining(c)).casefold()
    return re.findall(r'[^\W_]+', text, re.UNICODE)


def state_scopes(text):
    parts = tokens(text)
    found = [(i, i+len(name), name) for name in US_STATES for i in range(len(parts))
             if tuple(parts[i:i+len(name)]) == name]
    # West Virginia is one place, not also an independent mention of Virginia.
    return {name for start, end, name in found if not any(a <= start and end <= b and b-a > end-start
            for a, b, _ in found)}


def words(text):
    return {ALIASES.get(x, x) for x in tokens(text)
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
    # A passing mention buried in a long page is not evidence about its subject.
    # Require a named question anchor in the title; for questions without named
    # anchors require an informative question term there. Body terms still rank
    # documents that pass, but cannot make an unrelated headline relevant.
    title_subjects = (anchors - WEAK_ANCHORS) or anchors or terms
    if not matches or not title_subjects.intersection(title_terms):
        return {'score': 0, 'matched_terms': []}
    # State election evidence must name the same state in its headline. Body
    # mentions and a shared party cannot rescue a Michigan/national headline
    # for Kansas. Full phrases prevent New Mexico from matching Mexico and
    # West Virginia from matching Virginia. Abbreviations/districts are not
    # inferred here; this is a conservative lexical guard, not entity linking.
    scope = state_scopes(question) if ELECTORAL.intersection(tokens(question)) else set()
    if scope and not scope.intersection(state_scopes(title)):
        return {'score': 0, 'matched_terms': []}
    return {'score': len(matches) + 3 * len(terms & title_terms) + 2 * len(anchors & title_terms),
            'matched_terms': sorted(matches)}
