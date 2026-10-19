"""Four small causal hypotheses, using only completed M1 bars.

No volume column exists: the reversion benchmark is a time average, not VWAP.
"""
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from math import sqrt
from zoneinfo import ZoneInfo
from backtest.scalp_policy import NY, aware

FAMILIES = ('opening_range', 'failed_breakout', 'mean_reversion', 'compression')


@dataclass(frozen=True)
class Specification:
    family: str
    reward: float = 1.5
    stop_atr: float = 1.5
    min_stop_pips: float = 5.
    hold_minutes: int = 60

    def __post_init__(self):
        if self.family not in (*FAMILIES,'ict_baseline') or not 1 <= self.reward <= 3:
            raise ValueError('unknown family/reward')
        if not .5 <= self.stop_atr <= 3 or not 2 <= self.min_stop_pips <= 20:
            raise ValueError('invalid protective stop')
        if not 1 <= self.hold_minutes <= 120:
            raise ValueError('intraday holding cap required')


class Detector:
    def __init__(self, spec, bar_minutes=1):
        self.spec = spec
        if bar_minutes not in (1,5,15):
            raise ValueError('supported diagnostic resolutions are M1/M5/M15')
        self.bar_minutes = bar_minutes
        self.bars = deque(maxlen=121)
        self.or_bars = {}
        self.used_open = set()
        self.last_signal = None

    def update(self, bar):
        """bar.ts is OPEN time; decision is at bar.ts + one minute."""
        now = aware(bar['ts']) + timedelta(minutes=self.bar_minutes)
        if self.bars and aware(bar['ts']) != aware(self.bars[-1]['ts']) + timedelta(minutes=self.bar_minutes):
            self.bars.clear()
            self.or_bars.clear()
        prior = list(self.bars)
        self.bars.append(bar)
        session = None
        for name, zone, hour in (('london', ZoneInfo('Europe/London'), 8), ('ny', NY, 8)):
            local = now.astimezone(zone)
            minute = local.hour * 60 + local.minute
            key = (name, local.date())
            if hour*60 < minute <= hour*60+15:
                self.or_bars.setdefault(key, []).append(bar)
            if hour*60+15 <= minute < hour*60+120:
                session = key
        # Keep only today's opening ranges; bounded memory, no next-day carry.
        self.or_bars = {k: v for k, v in self.or_bars.items() if (now.date()-k[1]).days < 2}
        self.used_open = {k for k in self.used_open if (now.date()-k[1]).days < 2}
        if len(prior) < 120 or session is None:
            return None
        if self.last_signal and now-self.last_signal < timedelta(minutes=15):
            return None
        closes = [x['close'] for x in prior]
        tr = [max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close']))
              for a, b in zip(prior[:-1], prior[1:])]
        atr = sum(tr[-20:])/20
        long_atr = sum(tr)/len(tr)
        variation = sum(abs(b-a) for a,b in zip(closes[:-1], closes[1:]))
        efficiency = abs(closes[-1]-closes[0])/variation if variation else 0
        regime = 'high_volatility' if atr > 1.5*long_atr else 'trend' if efficiency >= .2 else 'range'
        top = max(x['high'] for x in prior[-30:])
        bottom = min(x['low'] for x in prior[-30:])
        c, sign = bar['close'], 0
        family = self.spec.family
        if family == 'opening_range':
            window = self.or_bars.get(session, [])
            if len(window) == 15//self.bar_minutes and session not in self.used_open:
                hi, lo = max(x['high'] for x in window), min(x['low'] for x in window)
                sign = 1 if closes[-1] <= hi < c else -1 if closes[-1] >= lo > c else 0
                if sign:
                    self.used_open.add(session)
        elif family == 'failed_breakout' and efficiency < .2:
            sign = -1 if bar['high'] > top and c < top and c < bar['open'] else (
                1 if bar['low'] < bottom and c > bottom and c > bar['open'] else 0)
        elif family == 'mean_reversion' and efficiency < .2:
            mean = sum(closes[-60:])/60
            sd = sqrt(sum((x-mean)**2 for x in closes[-60:])/60)
            # Directional turn while still >2 standard deviations displaced.
            sign = 1 if sd and c < mean-2*sd and c > bar['open'] else (
                -1 if sd and c > mean+2*sd and c < bar['open'] else 0)
        elif family == 'compression':
            short_hi = max(x['high'] for x in prior[-20:])
            short_lo = min(x['low'] for x in prior[-20:])
            wide = max(x['high'] for x in prior[-60:])-min(x['low'] for x in prior[-60:])
            if short_hi-short_lo < .6*wide and abs(c-bar['open']) >= atr:
                sign = 1 if c > short_hi else -1 if c < short_lo else 0
        if not sign or atr <= 0:
            return None
        self.last_signal = now
        distance = max(self.spec.min_stop_pips*.0001, self.spec.stop_atr*atr)
        return dict(sign=sign, stop=c-sign*distance, target=c+sign*self.spec.reward*distance,
                    decision_at=now, regime=regime, family=family)


class ICTDetector:
    """Explicit mechanical old-ICT detector, scored at 8; no default-family drift.

    The common engine replaces old fill/cost/risk mechanics, not the ICT signal
    levels. Only complete contiguous hours form H1 evidence. No LLM/network.
    """
    def __init__(self):
        self.m1=deque(maxlen=300)
        self.h1=deque(maxlen=300)
        self.bucket=[]
        self.seen=set()

    def update(self,bar):
        from brain.app import _detect_ict_candidate
        from brain.scoring.confluence import score_setup
        from brain.scheduler.killzone_clock import current_killzone
        now=aware(bar['ts'])+timedelta(minutes=1)
        self.m1.append(bar)
        hour=aware(bar['ts']).replace(minute=0,second=0,microsecond=0)
        if self.bucket and aware(self.bucket[-1]['ts'])+timedelta(minutes=1)!=aware(bar['ts']):
            self.bucket=[]
        if self.bucket and aware(self.bucket[0]['ts']).replace(minute=0)!=hour:
            self.bucket=[]
        self.bucket.append(bar)
        if len(self.bucket)==60 and aware(self.bucket[0]['ts'])==hour:
            self.h1.append(dict(ts=hour,open=self.bucket[0]['open'],close=bar['close'],
                high=max(x['high'] for x in self.bucket),low=min(x['low'] for x in self.bucket)))
            self.bucket=[]
        if not current_killzone(now):
            return None
        rows=[dict(x,index=i) for i,x in enumerate(self.m1)]
        candidate=_detect_ict_candidate(rows,dict(htf_candles=list(self.h1),now=now,pair=bar['pair']))
        if not candidate:
            return None
        score=score_setup(candidate['factors'],no_high_impact_news_next_15min=True)
        key=(bar['pair'],rows[candidate['mss']['index']]['ts'],candidate['entry'],candidate['stop_loss'])
        if score.get('reason') or score['score']<8 or key in self.seen:
            return None
        self.seen.add(key)
        return dict(sign=1 if candidate['direction']=='buy' else -1,stop=candidate['stop_loss'],
                    target=candidate['take_profit'],reference_entry=candidate['entry'],
                    decision_at=now,regime='unclassified_ict',family='ict_baseline')
