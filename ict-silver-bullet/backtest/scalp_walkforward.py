"""Predeclared rolling retrospective experiment; no live promotion.

Six calendar months tune, two validate; both reward variants logged. Reused
recent data is isolated until a final configuration is frozen, then opened in
one normal/stress evaluation batch. Never called automatically on import.
"""
import argparse
from collections import defaultdict
from dataclasses import replace, asdict
from datetime import timedelta
import json
import logging
from pathlib import Path
from statistics import mean
from backtest.scalp_signals import FAMILIES, Specification, Detector
from backtest.scalp_policy import Costs, aware
from backtest.scalp_engine import run
from backtest.scalp_analysis import metrics, monthly, kelly_cap, bootstrap, multiplicity_haircut, numerical_gates, robustness
from backtest.scalp_study import (read_data,stream,write_run,write_json,trial,finish_trial,
                                  RETROSPECTIVE_HOLDOUT,SOURCE_SHA256)
from brain.logging_setup import setup_logging


def add_months(ts,n):
    k=ts.year*12+ts.month-1+n
    return ts.replace(year=k//12,month=k%12+1,day=1,hour=0,minute=0,second=0,microsecond=0)


def folds(first,holdout):
    month=first.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    first_full=month if first==month else add_months(month,1)
    cursor=add_months(first_full,6)
    result=[]
    while cursor<holdout:
        result.append((max(first,add_months(cursor,-6)),cursor,min(add_months(cursor,2),holdout)))
        cursor=add_months(cursor,2)
    return result


class Scheduled:
    def __init__(self,family,schedule):
        self.models=defaultdict(lambda:{r:Detector(Specification(family,reward=r)) for r in (1.5,2.)})
        self.schedule=schedule

    def __call__(self,row):
        from datetime import timedelta
        now=aware(row['ts'])+timedelta(minutes=1)
        signals={r:m.update(row) for r,m in self.models[row['pair']].items()}
        chosen=next((reward for start,end,reward in self.schedule if start<=now<end),None)
        return signals.get(chosen)


def experiment(frames,audit,output,clock,news=(),simulations=2000):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    ledger=Path(__file__).resolve().parents[1]/'reports'/'trials_ledger.csv'
    if (ledger.parent/'frozen_holdout_receipt.json').exists():
        raise ValueError('holdout already frozen/opened; inspect the existing run before any recovery')
    write_json(output/'data_audit.json',audit)
    first=min(aware(x['first']) for x in audit)
    last=max(aware(x['last']) for x in audit)
    boundary=aware(RETROSPECTIVE_HOLDOUT)
    split=folds(first,boundary)
    if not split: raise ValueError('at least one six-month-tune/two-month-validation fold required')
    def execute(spec,settings,start,end,folder,stage,candidate=None):
        path=output/folder
        entry=trial(path,spec,settings,clock,start.isoformat(),end.isoformat(),stage,ledger)
        logging.getLogger('scalp-walkforward').info('START %s %s',stage,folder)
        try:
            report=run(stream(frames,start,end),spec,costs=settings,start=start,end=end,
                       news_events=news,news_complete=False,candidate=candidate)
            tags={m:stage for m in report['months']}
            write_run(path,report,tags)
        except Exception:
            finish_trial(entry,ledger,'FAILED'); raise
        finish_trial(entry,ledger,'COMPLETE')
        logging.getLogger('scalp-walkforward').info('COMPLETE %s trades=%s',folder,len(report['trades']))
        return report
    candidates=[]
    write_json(output/'predeclared.json',dict(families=FAMILIES,rewards=[1.5,2.],
        source=SOURCE_SHA256,clock_scenario=clock,clock_verified=False,news_complete=False,
        holdout=RETROSPECTIVE_HOLDOUT,holdout_status='previously examined retrospective holdout',
        sizing='0.05% small screening allocation; final risk requires day-bootstrap quarter-Kelly cap',
        prior_trial_count='unknown',promotion_allowed=False))
    for family in FAMILIES:
        schedule,walk=[] ,[]
        for i,(tune_start,valid_start,valid_end) in enumerate(split):
            choices=[]
            for reward in (1.5,2.):
                spec=Specification(family,reward=reward)
                rep=execute(spec,Costs(),tune_start,valid_start,f'{family}/fold_{i}/tune_{reward}','TUNE')
                stat=metrics(rep['trades'])
                choices.append((reward,stat))
                del rep
                tune_stress=execute(spec,Costs(multiplier=1.5),tune_start,valid_start,
                    f'{family}/fold_{i}/tune_{reward}_stress','TUNE')
                stat['stress']=metrics(tune_stress['trades'])
                del tune_stress
            # A negative choice may be evaluated diagnostically; it never
            # authorizes trading. This exposes failures rather than hiding them
            # behind a permanently flat, zero-trade validation portfolio.
            eligible=[(r,m) for r,m in choices if m['trades']>=30]
            chosen=max(eligible,key=lambda x:(x[1]['average_r'],-x[0]))[0] if eligible else None
            schedule.append((valid_start,valid_end,chosen))
            walk.append(dict(tune_start=tune_start,tune_end=valid_start,validate_end=valid_end,
                             selected_reward=chosen,tune_choices=choices,
                             selection='highest tune expectancy with >=30 trades; diagnostic only'))
        write_json(output/family/'walk_forward.json',walk)
        spec=Specification(family)
        report=execute(spec,Costs(),split[0][1],boundary,f'{family}/walk_forward','VALIDATE_RETROSPECTIVE',Scheduled(family,schedule))
        stress=execute(spec,Costs(multiplier=1.5),split[0][1],boundary,f'{family}/walk_forward_stress','VALIDATE_RETROSPECTIVE',Scheduled(family,schedule))
        stat,stressed=metrics(report['trades']),metrics(stress['trades'])
        cap=kelly_cap(report['trades'])
        stress_cap=kelly_cap(stress['trades'])
        cap['normal_cap']=cap['cap']; cap['stress_cap']=stress_cap['cap']
        cap['cap']=min(cap['cap'],stress_cap['cap'])
        frontier=[]
        if report['trades']:
            for risk in (.00025,.0005,.001,.0015,.0025):
                mc_entry=trial(output/family/'risk_frontier.json',spec,Costs(),clock,
                    split[0][1].isoformat(),boundary.isoformat(),'MONTE_CARLO',ledger,
                    risk=risk,extra=dict(block_days=5,seed=1701,simulations=simulations))
                frontier.append(dict(kelly_permitted=risk<=cap['cap'],**bootstrap(report['trades'],risk=risk,
                    first_day=split[0][1],last_day=boundary,simulations=simulations)))
                finish_trial(mc_entry,ledger,'COMPLETE')
                stress_entry=trial(output/family/'risk_frontier.json',spec,Costs(multiplier=1.5),clock,
                    split[0][1].isoformat(),boundary.isoformat(),'MONTE_CARLO',ledger,
                    risk=risk,extra=dict(block_days=5,seed=1701,simulations=simulations))
                frontier[-1]['stress']=bootstrap(stress['trades'],risk=risk,first_day=split[0][1],
                    last_day=boundary,simulations=simulations) if stress['trades'] else None
                finish_trial(stress_entry,ledger,'COMPLETE' if stress['trades'] else 'NO_STRESSED_TRADES')
        write_json(output/family/'risk_frontier.json',dict(kelly=cap,frontier=frontier))
        regimes={regime:metrics([t for t in report['trades'] if t['regime']==regime])
                 for regime in ('trend','range','high_volatility')}
        table=monthly(report)
        candidate=dict(family=family,metrics=stat,stress=stressed,kelly=cap,
            frontier=frontier,regimes=regimes,months=table,
            risk_recommendation=0.,original_lockbox_gate=False,clock_verified=False,
            news_complete=False,live_eligible=False)
        candidates.append(candidate)
        candidate['gates']=numerical_gates(candidate)
        write_json(output/family/'candidate.json',candidate)
        del report,stress
    # Selection uses only pre-holdout retrospective validation; no recent prices.
    def rank(x):
        numeric=[v for k,v in x['gates'].items() if k not in (
            'original_untouched_lockbox','source_clock_verified','historical_news_complete','parameter_robustness')]
        hit_rate=sum(m['hit_10pct'] and not m['breach_daily'] and not m['breach_overall']
                     for m in x['months'])/max(1,len(x['months']))
        return (all(numeric),hit_rate,x['metrics']['average_r'] if x['metrics']['average_r'] is not None else -float('inf'))
    best=max(candidates,key=rank)
    write_json(output/'candidate_comparison.json',candidates)
    # Freeze one diagnostic configuration. Prefer the last eligible rolling tune
    # choice, otherwise the predeclared smaller reward. Not a trading recommendation.
    walk=json.loads((output/best['family']/'walk_forward.json').read_text(encoding='utf-8'))
    reward=next((x['selected_reward'] for x in reversed(walk) if x['selected_reward']),1.5)
    chosen=Specification(best['family'],reward=reward)
    frozen=dict(strategy=asdict(chosen),screen_risk=.0005,recommended_live_risk=0.,
                data=SOURCE_SHA256,clock=clock,clock_verified=False,
                news_complete=False,evidence='retrospective; no independent holdout',
                holdout=RETROSPECTIVE_HOLDOUT)
    # Exclusive receipt prevents accidental repeated holdout selection or reruns.
    receipt=ledger.parent/'frozen_holdout_receipt.json'
    with receipt.open('x',encoding='utf-8') as f:
        json.dump(frozen,f,indent=2)
    write_json(output/'frozen_holdout_receipt.json',frozen)
    full_end=last+timedelta(minutes=1)
    for name,settings in (('normal',Costs()),('stress',Costs(multiplier=1.5))):
        rep=execute(chosen,settings,first,full_end,f'frozen_final/{name}','FROZEN_RETROSPECTIVE')
        tags={m:('RETROSPECTIVE_HOLDOUT' if m>='2026-07' else 'DEVELOPMENT_REUSED') for m in rep['months']}
        write_run(output/f'frozen_final/{name}',rep,tags)
        recent=dict(rep,months=[m for m in rep['months'] if m>='2026-07'],
            trades=[t for t in rep['trades'] if aware(t['closed_at'])>=boundary],
            initial=next((m['balance'] for m in rep['marks'] if aware(m['ts'])>=boundary),rep['initial']))
        write_run(output/f'retrospective_holdout/{name}',recent,tags)
        del rep,recent
        baseline=execute(Specification('ict_baseline',reward=2.),settings,first,full_end,
                         f'ict_baseline/{name}','BASELINE_RETROSPECTIVE')
        del baseline
    # Neighborhoods remain pre-holdout and diagnostic; cannot retune frozen choice.
    sensitivity=[]
    rewards=(chosen.reward-.25,chosen.reward,chosen.reward+.25)
    for stop in (1.25,1.5,1.75):
        for rr in rewards:
            rep=execute(replace(chosen,stop_atr=stop,reward=rr),Costs(),split[0][1],boundary,
                        f'sensitivity/stop_{stop}_reward_{rr}','SENSITIVITY_RETROSPECTIVE')
            sensitivity.append(dict(stop_atr=stop,reward=rr,**metrics(rep['trades'])))
            del rep
            rep=execute(replace(chosen,stop_atr=stop,reward=rr),Costs(multiplier=1.5),split[0][1],boundary,
                        f'sensitivity/stop_{stop}_reward_{rr}_stress','SENSITIVITY_RETROSPECTIVE')
            sensitivity[-1]['stress']=metrics(rep['trades'])
            del rep
    write_json(output/'sensitivity.json',sensitivity)
    shape=robustness(sensitivity,chosen.stop_atr,chosen.reward)
    best['gates']['parameter_robustness']=shape['passed']
    write_json(output/'robustness.json',shape)
    write_json(output/'candidate_comparison.json',candidates)
    write_json(output/best['family']/'candidate.json',best)
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    grid=[[next(x['average_r'] for x in sensitivity if x['stop_atr']==stop and x['reward']==rr)
           for rr in rewards] for stop in (1.25,1.5,1.75)]
    grid=[[float('nan') if x is None else x for x in row] for row in grid]
    fig,ax=plt.subplots(); image=ax.imshow(grid,origin='lower',aspect='auto')
    ax.set_xticks(range(3),rewards); ax.set_yticks(range(3),[1.25,1.5,1.75])
    ax.set_xlabel('Reward R'); ax.set_ylabel('Stop ATR multiplier')
    ax.set_title('Pre-holdout sensitivity: net R; modelled spreads')
    fig.colorbar(image,ax=ax); fig.tight_layout(); fig.savefig(output/'sensitivity.png'); plt.close(fig)
    import csv
    with ledger.open(newline='',encoding='utf-8') as f: entries=list(csv.DictReader(f))
    completed=[e for e in entries if e['status']=='COMPLETE']
    haircut=multiplicity_haircut([m['net_return_pct'] for m in best['months']],len(completed))
    write_json(output/'verdict.json',dict(best_family=best['family'],new_complete_runs=len(completed),
        historical_trial_count='unknown',multiple_testing=haircut,original_gates_passed=False,
        integration_permitted=False,monthly_target_supportable=None,
        reason='conditional retrospective research; source clock/news incomplete; independent lockbox gate unsatisfied'))
    document_final(output,best,candidates,frozen,walk,haircut,len(completed))
    return best


def document_final(output,best,candidates,frozen,walk,haircut,count):
    """Compile only executed results; unverified sources can never yield a pass."""
    from backtest.scalp_analysis import summary
    report=Path(__file__).resolve().parents[1]/'reports'/'final_report.md'
    lines=['# Retrospective scalping validation',
        '**No strategy passes the original acceptance gates. No deployment or live integration is permitted.**',
        'The best candidate below is a retrospective diagnostic, not a return forecast. Source clock and historical news are unverified; spreads are MODELLED.',
        '## Frozen diagnostic specification','```json',json.dumps(frozen,indent=2),'```',
        'Detailed signal, stop, target, regime and cost rules: `research/strategies.md`.',
        '## Candidate comparison','| Family | Trades | Net average R | PF | Stressed average R | +10% months |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for c in candidates:
        s=summary(c['months'])
        lines.append(f"| {c['family']} | {c['metrics']['trades']} | {c['metrics']['average_r']} | {c['metrics']['profit_factor']} | {c['stress']['average_r']} | {s.get('hit_10_count',0)}/{s['months']} |")
    lines+=['## Rolling selection for the best diagnostic',
            '| Tune start | Validate start | Validate end | Reward R |', '| --- | --- | --- | ---: |']
    for w in walk:
        lines.append(f"| {w['tune_start']} | {w['tune_end']} | {w['validate_end']} | {w['selected_reward']} |")
    lines+=['## Evidence and monthly tables',
        f"Artifact root: `{output.resolve()}`. Each run contains summary CSV/Markdown, monthly JSON/PNG, archived trades and floating-equity samples.",
        '`frozen_final/normal` and `frozen_final/stress`: full-history frozen post-selection comparison. These do not provide independent evidence.',
        f"`{best['family']}/walk_forward` and `walk_forward_stress`: rolling retrospective validation only, with continuous equity and breakers across folds.",
        '`retrospective_holdout/normal` and `stress`: previously examined July–October 2026 history, opened only after the diagnostic configuration was frozen in this request. It is not an untouched lockbox.',
        '`ict_baseline/normal` and `stress`: explicit mechanical ICT levels and score ≥8 on the common execution engine; no default trend-reclaim substitution.',
        '## Regimes','```json',json.dumps(best['regimes'],indent=2),'```',
        '## Bootstrap risk frontier',
        '30-calendar-day resampling with five-day blocks; within-trade floating-bound paths, empty days and the 2/3/5/7 policy retained. This conditions on observed opportunities and cannot identify missing gap tails.',
        '| Risk fraction | Kelly permitted | P(+10% at month end) | P(daily breach) | P(overall breach) | P(any breach) | Upper 95% P(any breach) | Median / p05 / p95 return % |',
        '| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |']
    for x in best['frontier']:
        r=x['monthly_return_pct']
        lines.append(f"| {x['risk']} | {x['kelly_permitted']} | {x['p_10pct']} | {x['p_daily_breach']} | {x['p_overall_breach']} | {x['p_union_breach']} | {x['p_union_breach_upper95']} | {r['median']} / {r['p05']} / {r['p95']} |")
    lines+=['Normal and +50% stressed frontiers, including max-drawdown distributions: `risk_frontier.json` under each family.',
        '## Sensitivity and multiple testing',
        'See `sensitivity.png` and `sensitivity.json`: 3×3 stop/reward neighborhood on pre-holdout data, including cost stress. No neighbor can replace the frozen holdout configuration.',
        f'{count} completed new evaluation runs are recorded in `reports/trials_ledger.csv`; Monte Carlo reriskings are separately labeled. The prior lifetime strategy-trial count is unknown.',
        'The following Bonferroni normal-mean diagnostic is an explicit multiple-testing haircut, not a certified deflated Sharpe ratio. Monthly serial dependence and unknown prior trials prevent a statistically clean pass.',
        '```json',json.dumps(haircut,indent=2),'```',
        '## Acceptance review','```json',json.dumps(best['gates'],indent=2),'```',
        'The predeclared robustness rule requires all eight neighbors to remain positive at normal/stressed costs, and median normal neighbor expectancy at least half the positive center. See `robustness.json`; review the heatmap as well. This is a diagnostic threshold, not an independent significance test.',
        '## Demo and manual deployment',
        'No deployment steps are authorized for this result. First verify the broker clock and point/spread metadata, restore a complete historical news calendar, and complete an untouched demo observation period of at least four complete calendar months. Log every signal, rejection, polling delay, fill, spread, floating-equity sample and breaker event. Reassess the full original gates, including sufficient trades and market-regime coverage, before any integration.',
        'After a separate approved pass: wire the frozen detector and shared limits into Brain, verify schema-compatible signal rows and executor behavior, run all tests, then deploy manually to demo only. No credentials belong in research artifacts.',
        '## Verdict',
        'The +10% monthly objective is not established. Observed retrospective hit rates are reported without relabeling development history as independent evidence. No forward monthly target is supportable from these conditional results alone; recommended live risk is zero. Research code was pushed at the user\'s request for offline execution on EC2; no strategy was deployed or connected to a live trading account.']
    formatted=[]
    for line in lines:
        formatted.extend(['',line,''] if line.startswith('#') else [line])
    report.write_text('\n'.join(formatted)+'\n',encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--output',type=Path,default=Path('reports/monthly/study'))
    p.add_argument('--clock',choices=('label_utc','broker_eu','broker_us'),required=True)
    p.add_argument('--news',type=Path)
    p.add_argument('--simulations',type=int,default=2000)
    args=p.parse_args()
    logger=setup_logging('scalp-walkforward')
    frames,audit=read_data(args.data_dir,args.clock)
    news=json.loads(args.news.read_text(encoding='utf-8')) if args.news else []
    best=experiment(frames,audit,args.output,args.clock,news,args.simulations)
    logger.info('Retrospective study complete; best diagnostic family=%s; live risk=0',best['family'])


if __name__=='__main__': main()
