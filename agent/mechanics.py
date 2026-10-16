"""Trusted seat-local facts and exact seven-player Werewolf calculations.

Free-form opponent messages are deliberately never parsed into hard facts.
Unknown observation formats remain available to the planner as raw evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from itertools import combinations
import re
from .evidence import speech_history, wolf_agreement


def role(value):
    return {'werewolf': 'wolf', 'wolf': 'wolf', 'seer': 'seer',
            'villager': 'villager', 'non-wolf': 'villager'}.get(str(value).lower())


@dataclass(frozen=True)
class Check:
    night: int | None
    target: int
    alignment: str


def outcome(alive, wolves, elimination, streak=0, resigned=False):
    """Assume an ordinary successful next-night kill, as in the strategy table."""
    n, w = alive, wolves
    if elimination == 'wolf':
        n, w = n - 1, w - 1
    elif elimination == 'town':
        n -= 1
    if w == 0:
        return {'winner': 'town', 'when': 'today'}
    if w >= n - w:
        return {'winner': 'wolves', 'when': 'today'}
    if elimination == 'none' and not resigned and streak is not None and streak + 1 >= 3:
        return {'winner': 'wolves', 'when': 'today', 'cause': 'stalling'}
    n -= 1
    if w >= n - w:
        return {'winner': 'wolves', 'when': 'tonight'}
    return {'winner': None, 'next_day_alive': n, 'next_day_wolves': w}


def tally(ballots):
    """Resolve completed seat-index ballots; 7 is abstention."""
    counts = {}
    for target in ballots:
        if type(target) is int and 0 <= target < 7:
            counts[target] = counts.get(target, 0) + 1
    leaders = [s for s, n in counts.items() if n == max(counts.values(), default=0)]
    return {'counts': counts, 'eliminated': leaders[0] if len(leaders) == 1 else None}


def coalition_bounds(alive, commitments):
    """Exact plurality bounds CONDITIONAL on the listed promises being honored.

    Uncommitted players may vote adversarially. Nobody can vote for themself.
    A promised vote is never promoted to an observed ballot.
    """
    alive = set(alive)
    fixed = {s: t for s, t in commitments.items()
             if s in alive and t != s and (t in alive or t == 7)}
    counts = tally(fixed.values())['counts']
    unknown = alive - fixed.keys()
    bounds = {t: {'minimum': counts.get(t, 0),
                  'maximum': counts.get(t, 0) + len(unknown - {t})} for t in sorted(alive)}
    guaranteed = [t for t in alive if bounds[t]['minimum'] > max(
        (bounds[r]['maximum'] for r in alive - {t}), default=0)]
    return {'conditional_on_promises': True, 'commitments': fixed,
            'vote_bounds': bounds, 'guaranteed_elimination': guaranteed[0] if guaranteed else None}


class Knowledge:
    def __init__(self, seat):
        self.seat = seat
        self.names = {}
        self.alive = set()
        self.alive_known = False
        self.roles = {}
        self.checks = []
        self.errors = []
        self.day = 0
        self.phase = None
        self.streak = None
        self.resigned_today = False
        self.records = []
        self.revealed_on = {}
        self.speech = {}
        self.agreement = {}

    def review(self, transcript):
        self.speech = speech_history(self, transcript)
        self.agreement = wolf_agreement(self, transcript)

    def resolve(self, value, *, display=False):
        if type(value) is int and 0 <= value < 7:
            return value
        if isinstance(value, str):
            if display and value in self.names:
                return self.names[value]
            match = re.fullmatch(r'Player([0-6])|([0-6])', value)
            if match:
                return int(match.group(1) or match.group(2))
            if value in self.names:
                return self.names[value]
        return None

    def _role(self, seat, value):
        value = role(value)
        if seat is None or value is None:
            return
        old = self.roles.get(seat)
        if old is not None and old != value:
            error = f'Conflicting observed roles for seat {seat}'
            if error not in self.errors:
                self.errors.append(error)
        else:
            self.roles[seat] = value

    def _check(self, night, target, alignment):
        target, alignment = self.resolve(target), role(alignment)
        if target is None or target == self.seat or alignment is None:
            return
        if night is not None and (type(night) is not int or night < 1):
            return
        alignment = 'wolf' if alignment == 'wolf' else 'villager'
        check = Check(night, target, alignment)
        if any(c.target == target and c.alignment != alignment for c in self.checks):
            error = f'Conflicting observed checks for seat {target}'
            if error not in self.errors:
                self.errors.append(error)
        if check not in self.checks:
            self.checks.append(check)

    def update(self, state):
        public = state.raw.get('game_state') or {}
        for player in state.raw.get('players', []):
            if type(player.get('position')) is int:
                self.names[player['name']] = player['position']
        self.day, self.phase = public.get('day', 0), public.get('phase')
        if 'alive' in public:
            resolved = [self.resolve(s, display=True) for s in public['alive']]
            self.alive_known = all(s is not None for s in resolved)
            self.alive = {s for s in resolved if s is not None}
        for death in public.get('dead', []):
            if isinstance(death, dict):
                seat = self.resolve(death.get('name', death.get('player', death.get('position'))), display=True)
                self._role(seat, death.get('role'))
                if seat is not None and role(death.get('role')) is not None:
                    self.revealed_on.setdefault(seat, death.get('day', self.day))
        # Only an explicitly supplied counter is trusted; missing is not zero.
        self.streak = next((public[k] for k in ('no_elimination_streak', 'days_without_elimination',
                           'consecutive_no_elimination_days') if type(public.get(k)) is int), None)
        self.resigned_today = any(isinstance(d, dict) and d.get('cause') == 'resigned'
                                  and d.get('day') == self.day for d in public.get('dead', []))
        record = {'day': self.day, 'dead': public.get('dead', []),
                  'vote_history': public.get('vote_history', [])}
        if not self.records or record != self.records[-1]:
            self.records.append(deepcopy(record))
        observation = state.raw.get('observation', state.observation)
        if isinstance(observation, dict):
            self._role(self.seat, observation.get('role'))
            # These are personal observation fields only, never chat or LLM output.
            for check in observation.get('investigations', []):
                if isinstance(check, dict):
                    self._check(check.get('night'), check.get('target'), check.get('result'))
            if self.roles.get(self.seat) == 'wolf':
                for partner in observation.get('wolf_partners', []):
                    self._role(self.resolve(partner), 'wolf')
        elif isinstance(observation, str):
            # Narrow, anchored formats: unrecognized text is not guessed.
            for line in observation.splitlines():
                match = re.match(r'(?:You are Player([0-6])\. )?Your role: (wolf|werewolf|seer|villager)(?:\.|$)', line.strip(), re.I)
                if match:
                    if match[1] is None or int(match[1]) == self.seat:
                        self._role(self.seat, match[2])
                    elif 'Observation seat mismatch' not in self.errors:
                        self.errors.append('Observation seat mismatch')
                match = re.fullmatch(r'Night (\d+): Player([0-6]) (?:is|was) (wolf|villager|non-wolf)\.', line.strip(), re.I)
                if match:
                    self._check(int(match[1]), int(match[2]), match[3])
        # Night actors are authoritative, unlike another player's role claim.
        if self.phase == 'night_seer' and state.legal_actions:
            self._role(self.seat, 'seer')
        if self.phase == 'night_wolf' and state.legal_actions:
            self._role(self.seat, 'wolf')
            if self.alive_known and all(a.action_id in list('0123456') for a in state.legal_actions):
                nonwolves = {int(a.action_id) for a in state.legal_actions if a.action_id in list('0123456')}
                for partner in self.alive - nonwolves - {self.seat}:
                    self._role(partner, 'wolf')

    def pairs(self):
        pairs = []
        for pair in combinations(range(7), 2):
            if any((s in pair) != (r == 'wolf') for s, r in self.roles.items()):
                continue
            if any((c.target in pair) != (c.alignment == 'wolf') for c in self.checks):
                continue
            pairs.append(pair)
        return pairs

    def certain(self):
        pairs = self.pairs()
        if not pairs or self.errors:
            return set(), set()
        wolves = set.intersection(*(set(p) for p in pairs))
        town = set(range(7)) - set.union(*(set(p) for p in pairs))
        return wolves, town

    def candidates(self, actions):
        """Filter factual contradictions, not merely unlikely tactics."""
        if self.errors or not self.pairs():
            return list(actions)
        _, town = self.certain()
        if self.phase == 'day_vote' and self.roles.get(self.seat) in ('seer', 'villager'):
            filtered = [a for a in actions if a.action_id == '7' or self.resolve(a.action_id) not in town]
            return filtered or list(actions)
        if self.phase == 'night_seer':
            checked = {c.target for c in self.checks}
            filtered = [a for a in actions if self.resolve(a.action_id) not in checked]
            return filtered or list(actions)
        return list(actions)

    def report(self):
        pairs = self.pairs()
        wolves, town = self.certain()
        counts = sorted({len(set(p) & self.alive) for p in pairs}) if self.alive_known else []
        clocks = []
        if self.alive_known:
            for count in counts:
                clocks.append({'wolves_alive': count, 'outcomes': {
                    e: outcome(len(self.alive), count, e, self.streak, self.resigned_today)
                    for e in ('wolf', 'town', 'none')}})
        completed = {}
        for record in self.records:
            history = record['vote_history']
            if not isinstance(history, list):
                continue
            for entry in history:
                if not isinstance(entry, dict) or not isinstance(entry.get('votes'), dict):
                    continue
                if entry.get('complete') is False or entry.get('status') in ('pending', 'collecting'):
                    continue
                if type(entry.get('day')) is not int or entry['day'] < 1 or entry['day'] > self.day:
                    continue
                votes = {}
                for sender, target in entry['votes'].items():
                    sender = self.resolve(sender, display=True)
                    if isinstance(target, str) and target in self.names:
                        target = self.resolve(target, display=True)
                    else:
                        target = 7 if target is None or target in ('abstain', 'Abstain', 7, '7') else self.resolve(target, display=True)
                    if sender is None or target is None or sender in votes or sender == target:
                        break
                    votes[sender] = target
                else:
                    # Preserve recorded votes, but resigned targets do not count.
                    resigned = {self.resolve(d.get('name', d.get('player', d.get('position'))), display=True)
                                for r in self.records for d in r['dead'] if isinstance(d, dict)
                                and d.get('cause') == 'resigned' and d.get('day') == entry['day']}
                    result = {'day': entry['day'], 'votes': votes,
                              **tally(7 if t in resigned else t for t in votes.values())}
                    old = completed.get(entry['day'])
                    if old and (old['votes'] != votes or old.get('history_revised')):
                        result['history_revised'] = True
                    completed[entry['day']] = result
        return {'own_role': self.roles.get(self.seat), 'verified_roles': self.roles,
                'day': self.day, 'phase': self.phase,
                'personal_checks': [asdict(c) for c in self.checks],
                'alive_seats': sorted(self.alive), 'alive_mapping_complete': self.alive_known,
                'possible_original_wolf_pairs': pairs, 'certain_wolves': sorted(wolves),
                'certain_town': sorted(town), 'elimination_clock': clocks,
                'no_elimination_streak': self.streak, 'fact_errors': self.errors,
                'unparsed_role': self.seat not in self.roles,
                'strict_majority_sufficient': len(self.alive) // 2 + 1 if self.alive_known else None,
                'assumptions': ['Night kill succeeds', 'Announced votes are not ballots',
                                'Pair counts are possibilities, not calibrated probabilities'],
                'completed_records': self.records, 'completed_tallies': list(completed.values()),
                'attributed_speech': self.speech, 'wolf_agreement': self.agreement}
