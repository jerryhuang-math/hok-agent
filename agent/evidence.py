"""Attributed speech, revised intentions, and explicit private wolf agreements.

These records are claims and promises. They never become role facts or ballots.
Only narrow first-person forms are machine-verified; ambiguous language remains
in the transcript for tactical interpretation without entering vote counts.
"""
import re


# Refuse quoted, hypothetical, negated and secondhand statements as direct reports.
RELAY = re.compile(r'["“”>]|\b(?:said|says|claimed|claims|according|if|suppose|denies|not voting)\b', re.I)
VOTE = re.compile(r'^(?:My (?:provisional |intended |final )?vote is|I (?:now |will |intend to )?vote(?: for)?|I (?:am changing|change|switch) my vote to) Player([0-6])(?:[.,;!]|$)', re.I)
ABSTAIN = re.compile(r'^(?:I (?:will |intend to )?abstain|My (?:intended )?vote is abstention)(?:[.,;!]|$)', re.I)
WITHDRAW = re.compile(r'^(?:I withdraw my (?:vote|commitment)|I am undecided|My vote is undecided)(?:[.,;!]|$)', re.I)
CHECK = re.compile(r'^Night (\d+): Player([0-6]) (?:was|is) (wolf|non-wolf|villager)(?:[.,;!]|$)', re.I)


def clauses(content):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', content) if s.strip()]


def direct_clauses(content):
    # Remove entire quoted spans before splitting, so multi-sentence quotations
    # cannot acquire a new speaker when a quote crosses a sentence boundary.
    stripped = re.sub(r'"[^"]*"|“[^”]*”', ' ', content, flags=re.S)
    if any(q in stripped for q in ('"', '“', '”')):
        return []
    stripped = re.sub(r'^\s*>.*$', '', stripped, flags=re.M)
    return [s for s in clauses(stripped) if not RELAY.search(s)]


def direct_intention(content):
    """Last explicit first-person intention in a single message wins."""
    result = None
    for sentence in direct_clauses(content):
        match = VOTE.match(sentence)
        if match:
            result = {'target': int(match[1]), 'quote': sentence,
                      'kind': 'provisional' if 'provisional' in sentence.lower() else 'final' if 'final' in sentence.lower() else 'intended'}
        elif ABSTAIN.match(sentence):
            result = {'target': 7, 'quote': sentence}
        elif WITHDRAW.match(sentence):
            result = {'target': None, 'quote': sentence}
    return result


def speech_history(knowledge, transcript):
    history, latest, reports, seer_claims = [], {}, [], []
    for message in sorted(transcript, key=lambda m: (m['day'], m.get('index', 0))):
        if message.get('type', 'chat') != 'chat':
            continue
        intention = direct_intention(message['content'])
        if intention is not None:
            entry = {**intention, 'sender': message['sender'], 'day': message['day'],
                     'evidence_id': message['evidence_id'], 'recipients': message.get('recipients', [])}
            history.append(entry)
            if message['day'] == knowledge.day:
                latest[message['sender']] = entry
        statements = direct_clauses(message['content'])
        if any(re.match(r"^I(?: am|'m) (?:the )?seer(?:[.!;]|$)", s, re.I) for s in statements):
            seer_claims.append({'sender': message['sender'], 'day': message['day'], 'evidence_id': message['evidence_id']})
        for sentence in statements:
            match = CHECK.match(sentence)
            if match and 1 <= int(match[1]) <= message['day']:
                reports.append({'reporter': message['sender'], 'night': int(match[1]),
                    'target': int(match[2]), 'alignment': 'wolf' if match[3].lower() == 'wolf' else 'villager',
                    'day': message['day'], 'evidence_id': message['evidence_id'], 'quote': sentence})
    revisited = []
    for report in reports:
        reporter = report['reporter']
        if knowledge.roles.get(reporter) != 'seer' or reporter in knowledge.alive:
            continue
        if report['day'] > knowledge.revealed_on.get(reporter, knowledge.day):
            continue
        conflict = any(r['reporter'] == reporter and r['target'] == report['target']
                       and r['alignment'] != report['alignment'] for r in reports)
        conflict |= any(r['reporter'] == reporter and r['night'] == report['night']
                        and r['target'] != report['target'] for r in reports)
        known = knowledge.roles.get(report['target'])
        conflict |= known is not None and (known == 'wolf') != (report['alignment'] == 'wolf')
        revisited.append({**report, 'credibility': 'disputed' if conflict else 'strong',
                          'basis': 'Attributable report by a revealed seer; assumes honest town reporting'})
    clear = sorted({r['target'] for r in revisited if r['credibility'] == 'strong' and r['alignment'] == 'villager'})
    accused = sorted({r['target'] for r in revisited if r['credibility'] == 'strong' and r['alignment'] == 'wolf'})
    living_reports = []
    for report in reports:
        if (report['reporter'] not in knowledge.alive or report['target'] not in knowledge.alive
                or report['alignment'] != 'wolf'
                or not any(c['sender'] == report['reporter'] and c['day'] <= report['day'] for c in seer_claims)):
            continue
        disputed = any(r['reporter'] == report['reporter'] and
                       ((r['target'] == report['target'] and r['alignment'] != report['alignment'])
                        or (r['night'] == report['night'] and r['target'] != report['target'])) for r in reports)
        living_reports.append({**report, 'credibility': 'disputed' if disputed else 'unverified_specific_check'})
    grouped = {}
    for report in living_reports:
        key = (report['reporter'], report['night'], report['target'], report['alignment'])
        if key not in grouped:
            grouped[key] = {**report, 'evidence_ids': []}
        grouped[key]['evidence_ids'].append(report['evidence_id'])
    living_reports = list(grouped.values())
    current = [e for s, e in latest.items() if s in knowledge.alive and e['target'] is not None
               and e['target'] != s and (e['target'] in knowledge.alive or e['target'] == 7)]
    support = {s: [e['sender'] for e in current if e['target'] == s] for s in sorted(knowledge.alive)}
    return {'commitment_history': history, 'latest_intentions': list(latest.values()),
            'current_commitments': current, 'announced_support': support,
            'revisited_seer_reports': revisited, 'strongly_cleared': clear,
            'seer_claims': seer_claims, 'living_wolf_reports': living_reports,
            'strongly_accused': accused, 'warning': 'Promises are not ballots; report credibility is not a hard role fact'}


PROPOSAL = re.compile(r'^Wolf plan (D(\d+)-P([0-6])-V(\d+)): vote (Player([0-6])|abstain); night (none|Player[0-6](?:, Player[0-6])*)\.(?: |$)')
ACK = re.compile(r'^Confirm (D\d+-P[0-6]-V\d+)\.(?: |$)')


def wolf_agreement(knowledge, transcript):
    wolves, _ = knowledge.certain()
    partners = (wolves & knowledge.alive) - {knowledge.seat}
    empty = {'status': 'unknown_partner', 'selected': None, 'conflicts': []}
    if knowledge.roles.get(knowledge.seat) != 'wolf' or len(partners) != 1:
        return empty
    pair = {knowledge.seat, next(iter(partners))}
    proposals, acknowledgements = {}, []
    valid_days = {knowledge.day}
    if knowledge.phase == 'night_wolf':
        valid_days.add(knowledge.day - 1)
    for m in transcript:
        if (m.get('type', 'chat') != 'chat' or m['day'] not in valid_days or m['sender'] not in pair
                or m.get('recipients') != list(pair - {m['sender']})):
            continue
        match = PROPOSAL.match(m['content'])
        if match and int(match[2]) == m['day'] and int(match[3]) == m['sender']:
            target = 7 if match[5] == 'abstain' else int(match[6])
            kills = [int(s) for s in re.findall(r'Player([0-6])', match[7])]
            if target in pair or (knowledge.phase != 'night_wolf' and target != 7 and target not in knowledge.alive):
                continue
            if any(s in wolves for s in kills):
                continue
            proposals[match[1]] = {'id': match[1], 'author': m['sender'], 'version': int(match[4]),
                'vote': target, 'night': kills, 'evidence_id': m['evidence_id'], 'day': m['day'],
                'index': m.get('index', 0)}
        ack = ACK.match(m['content'])
        if ack:
            acknowledgements.append((ack[1], m['sender'], m['day'], m.get('index', 0)))
    confirmed = [p for p in proposals.values() if any(
        ident == p['id'] and sender != p['author'] and (day, index) > (p['day'], p['index'])
        for ident, sender, day, index in acknowledgements)]
    # Both seats use the same tie-break when simultaneous proposals disagree.
    choices = confirmed or list(proposals.values())
    if choices:
        latest_day = max(p['day'] for p in choices)
        choices = [p for p in choices if p['day'] == latest_day]
    selected = min(choices, key=lambda p: (p['author'], -p['version'])) if choices else None
    conflicts = [p['id'] for p in choices if selected and (p['vote'], p['night']) != (selected['vote'], selected['night'])]
    return {'status': 'agreed' if confirmed else 'proposed' if selected else 'none',
            'selected': selected, 'conflicts': conflicts, 'partner': next(iter(partners))}
