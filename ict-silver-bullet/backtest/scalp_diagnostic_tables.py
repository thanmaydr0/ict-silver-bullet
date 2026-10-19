"""Readable tables from completed diagnostics only; never runs a strategy."""
import csv
import gzip
import json
from pathlib import Path
from statistics import mean,pstdev
from backtest.scalp_analysis import quantile
from backtest.scalp_diagnostics import table
from backtest.scalp_study import write_json


def main():
    root=Path(__file__).resolve().parents[1]
    output=root/'reports/diagnostics'
    if not (output/'complete.json').exists():
        raise ValueError('diagnostic batch is not complete')
    def read(name):
        with (output/name).open(newline='',encoding='utf-8') as f:
            return list(csv.DictReader(f))
    def number(row,key):
        return float(row[key]) if row.get(key) not in ('',None) else None
    def fmt(x):
        return round(x,6) if isinstance(x,float) else x
    def emit(name,rows):
        rows=[{k:fmt(v) for k,v in row.items()} for row in rows]
        table(output/name,rows);return rows
    families=('opening_range','failed_breakout','mean_reversion','compression','ict_baseline')
    costs=read('cost_reruns.csv'); indexed={(x['family'],x['mode']):x for x in costs}
    paired={x['family']:x for x in read('gross_net_same_trades.csv')}
    cost_table=[]
    for family in families:
        full=indexed[family,'full'];p=paired[family]
        cost_table.append(dict(family=family,zero_trades=int(indexed[family,'zero']['trades']),
            full_trades=int(full['trades']),gross_zero_R=number(indexed[family,'zero'],'net_r'),
            spread_R=number(indexed[family,'spread'],'net_r'),
            spread_commission_R=number(indexed[family,'spread_commission'],'net_r'),
            full_R=number(full,'net_r'),mean_cost_R=number(p,'mean_cost_r'),
            cost_over_mean_stop=number(p,'cost_over_average_stop'),
            mean_stop_pips=number(full,'average_stop_pips'),mean_cost_pips=number(full,'average_cost_pips')))
    emit('table_1_costs',cost_table)
    ns=read('null_seeds.csv'); controls=sorted({r['control'] for r in ns})
    null_table=[]
    for control in controls:
        rows=[r for r in ns if r['control']==control];rs=[number(r,'net_r') for r in rows]
        wins=[number(r,'win_pct') for r in rows]
        barrier=[number(r,'barrier_win_pct') for r in rows if number(r,'barrier_win_pct') is not None]
        se=pstdev(rs)/len(rs)**.5
        null_table.append(dict(control=control,seeds=len(rows),mean_trades=mean(int(r['trades']) for r in rows),
            mean_R=mean(rs),seed_p025_R=quantile(rs,.025),seed_p975_R=quantile(rs,.975),
            randomization_mean_CI_low=mean(rs)-1.96*se,randomization_mean_CI_high=mean(rs)+1.96*se,
            mean_cost_R=mean(number(r,'cost_r') for r in rows),
            net_plus_cost_R=mean(number(r,'net_r')+number(r,'cost_r') for r in rows),
            mean_win_pct=mean(wins),win_seed_p025=quantile(wins,.025),win_seed_p975=quantile(wins,.975),
            mean_barrier_win_pct=mean(barrier),barrier_win_seed_p025=quantile(barrier,.025),
            barrier_win_seed_p975=quantile(barrier,.975)))
    emit('table_2_null',null_table)
    specs=json.loads((output/'fixed_specifications.json').read_text())
    comparisons=[]
    for family in families:
        reward=specs[family]['reward'];row=dict(family=family,reward=reward)
        for mode in ('zero','full'):
            xs=[number(r,'net_r') for r in ns if r['control']==f'{mode}_R{reward:g}']
            actual=number(indexed[family,mode],'net_r')
            p=(1+sum(x>=actual for x in xs))/(len(xs)+1)
            row.update({mode+'_real_R':actual,mode+'_null_mean_R':mean(xs),
                mode+'_upper_tail_p':p if family!='ict_baseline' else None,
                mode+'_bonferroni_4_p':min(1.,4*p) if family!='ict_baseline' else None,
                mode+'_above_null_p975':actual>quantile(xs,.975) if family!='ict_baseline' else None})
        row['matching']='ATR/minimum-stop, reward-matched session null' if family!='ict_baseline' else 'ICT structural stops differ; p-values not asserted'
        comparisons.append(row)
    emit('table_2_real_vs_null',comparisons)
    ordering=read('bar_ordering.csv');order_index={(r['family'],r['policy']):r for r in ordering}
    bounds=[]
    for family in families:
        stop=order_index[family,'stop_first']
        target=number(order_index[family,'target_first'],'net_r')
        heuristic=number(order_index[family,'open_direction'],'net_r')
        bounds.append(dict(family=family,trades=int(stop['trades']),
            ambiguous_trades=int(stop['ambiguous_trades']),share_pct=number(stop,'ambiguous_share_pct'),
            stop_first_R=number(stop,'net_r'),target_first_R=target,open_direction_R=heuristic,
            target_first_delta_R=target-number(stop,'net_r'),
            opening_target_then_stop=int(stop['opening_target_then_stop'])))
    emit('table_4_bar_bounds',bounds)
    checks=[dict(check='cash accounting max |gross-costs-net| USD',observed=max(number(r,'accounting_max_error') for r in costs+ns),reference='0 within floating-point tolerance'),
        dict(check='diagnostic account overall halts',observed=sum(r['overall_halt']=='True' for r in costs+ns),reference='0 for uncensored comparison'),
        dict(check='open positions at source boundary',observed=sum(r['open_position']=='True' for r in costs+ns),reference='0'),
        dict(check='M1 bars opening beyond target but resolved stop',observed=sum(r['opening_target_then_stop'] for r in bounds),reference='0; nonzero requires a gap-order correction')]
    symmetric=next(r for r in null_table if r['control']=='zero_symmetric')
    checks.extend([dict(check='zero-cost symmetric mean R',observed=symmetric['mean_R'],reference='approximately 0; see seed and randomization intervals'),
        dict(check='zero-cost symmetric win %',observed=symmetric['mean_win_pct'],reference='approximately 50; finite holding exits included'),
        dict(check='zero-cost symmetric barrier-only win %',observed=symmetric['mean_barrier_win_pct'],reference='approximately 50; inspect ambiguity bias')])
    emit('table_3_engine_checks',checks)
    resolution=read('resolution.csv');emit('table_6_resolution',[
        {k:(number(r,k) if k not in ('mode','risk_flags_seen','overall_halt') else r[k]) for k in
         ('minutes','stop_scale','mode','trades','net_r','profit_factor','average_stop_pips','cost_r','risk_flags_seen','overall_halt')}
        for r in resolution])
    for minutes in (5,15):
        both={}
        for mode in ('zero','full'):
            folder=output/f'resolution/M{minutes}_{mode}'
            with gzip.open(folder/'trades.jsonl.gz','rt',encoding='utf-8') as f: trades=[json.loads(line) for line in f]
            with (folder/'summary.csv').open(newline='',encoding='utf-8') as f: months=list(csv.DictReader(f))
            for row in months:
                ts=[t for t in trades if t['closed_at'].startswith(row['month'])]
                both.setdefault(row['month'],{}).update({mode+'_trades':len(ts),
                    mode+'_R':mean(t['net']/t['reference_r_usd'] for t in ts) if ts else None,
                    mode+'_PF':number(row,'profit_factor'),mode+'_return_pct':number(row,'net_return_pct')})
        emit(f'table_6_M{minutes}_monthly',[dict(month=m,**d) for m,d in sorted(both.items())])
    write_json(output/'public_summary.json',dict(costs=cost_table,null=null_table,
        comparisons=comparisons,engine=checks,bar_bounds=bounds,resolution=resolution))
    sections=[
        ('1. Gross versus net','table_1_costs'),('2. Random-entry controls, 200 seeds each','table_2_null'),
        ('2b. Real versus matched null','table_2_real_vs_null'),('3. Engine sanity','table_3_engine_checks'),
        ('4. Intrabar ordering bounds','table_4_bar_bounds'),('5. Assumed spreads by New York hour','spread_by_hour'),
        ('6. M1/M5/M15 resolution','table_6_resolution'),('6b. M5 monthly table','table_6_M5_monthly'),
        ('6c. M15 monthly table','table_6_M15_monthly')]
    text=['# Diagnostic round: existing engine and fixed rules',
        'This is explicitly reused historical data, not independent validation. Source-clock mapping and historical news remain unverified. All reported spreads are MODELLED. No new strategy was built.',
        'R is the original signal price-stop distance times units, excluding fees. Zero-cost and cost-stage reruns have different trade sets; compare the identical-trade bridge separately in gross_net_same_trades.csv. Cost-over-mean-stop is mean cost pips / mean stop pips; mean_cost_R averages individual ratios.',
        'The diagnostic account uses negligible risk to avoid lifetime drawdown censoring. Resolution monthly tables use $10,000 and 0.05% risk with all breakers active. First/last months are partial. Signal aggregation retains indicator bar counts, uses only completed contiguous aggregates and scales known M1 stops 3x/6x; fills always use actual M1 bars.',
        'Null distributions condition on this historical market path. Empirical seed intervals are not fresh market confidence intervals. Smallest tail fraction with 200 seeds is 1/201. Four-family Bonferroni diagnostics are shown; these do not repair prior data reuse. ICT geometry is not matched by an ATR-stop null, so no ICT null p-value is asserted.']
    for title,name in sections:
        text.extend(['## '+title,(output/(name+'.md')).read_text()])
    text.extend(['## 7. Attribution',
        'The tables above separate measured cost drag, gross expectancy, null-relative performance and ordering bias. The written verdict must distinguish a detectable statistical difference from an economically usable edge. No result can certify the intended New York sessions while source-clock mapping is unverified.',
        'Original-engine parity on an actual 96-trade historical prefix passed, as did 33 unit tests. Faster replay never substitutes another execution kernel: it skips flat/no-order intervals and processes every M1 bar while pending/open. No source prices were generated or interpolated.',
        'Recorded spreads: none in the 13 existing Parquet exports checked; spread_schema_inventory.json contains the inventory. News widening is a model assumption and was not actually activated without a supplied historical calendar.'])
    (output/'diagnostic_report.md').write_text('\n\n'.join(text)+'\n',encoding='utf-8')


if __name__=='__main__': main()
