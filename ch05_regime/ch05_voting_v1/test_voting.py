import unittest
from datetime import date, timedelta
import market_regime_voting as m

class VotingTests(unittest.TestCase):
    def setUp(self):
        self.d = date(2026,11,12)
        days = [self.d-timedelta(days=i) for i in range(119,-1,-1)]
        self.data = {k:list(zip(days,[100+i for i in range(120)])) for k in m.SYMBOLS}
        self.data['vix'] = [(d,15) for d in days]
        self.data['vix3m'] = [(d,18) for d in days]
        self.data['gold'] = [(d,100) for d in days]
        self.data['pcr'] = [(self.d,1.3)]
        self.data['yield_spread'] = [(self.d,.4)]
        self.data['breadth'] = [(d, 100+i*2,100) for i,d in enumerate(days)]
    def test_net_boundaries_and_overlap(self):
        for b,s,w in [(3,0,'BULL'),(2,0,'NEUTRAL'),(4,3,'NEUTRAL'),(3,3,'NEUTRAL'),(0,1,'NEUTRAL'),(0,2,'RISK_OFF'),(1,4,'BEAR'),(5,2,'BULL')]:
            self.assertEqual(m.classify(b,s),w)
        for b in range(15):
            for s in range(15):
                net=b-s
                want='BULL' if net>=3 else 'BEAR' if net<=-3 else 'RISK_OFF' if net==-2 else 'NEUTRAL'
                self.assertEqual(m.classify(b,s),want)
    def test_all_analysts(self):
        r=m.evaluate(self.data,self.d,m.DEFAULTS)
        self.assertTrue(r['complete']); self.assertEqual(r['coverage'],11)
        a=r['analysts']; self.assertEqual((a[0]['bull'],a[0]['bear']),(2,0))
        self.assertEqual(a[3]['bull'],1); self.assertEqual(a[5]['bull'],1)
        self.assertEqual(a[6]['bull'],1); self.assertEqual(a[10]['bear'],1)
    def test_cumulative_bear_votes(self):
        self.data['taiex'][-1]=(self.d,50)
        a=m.evaluate(self.data,self.d,m.DEFAULTS)['analysts'][0]
        self.assertEqual(a['bear'],3)
    def test_missing_is_not_neutral(self):
        del self.data['pcr']
        r=m.evaluate(self.data,self.d,m.DEFAULTS)
        self.assertEqual(r['regime'],'INCOMPLETE'); self.assertEqual(r['coverage'],10)
    def test_stale(self):
        self.data['pcr']=[(self.d-timedelta(days=8),1.3)]
        self.assertFalse(m.evaluate(self.data,self.d,m.DEFAULTS)['complete'])
    def test_vix_boundaries(self):
        for v,b,s in [(15,1,0),(16,0,0),(19.9,0,0),(20,0,1),(28,0,1),(28.1,0,2)]:
            self.data['vix']=[(self.d,v)]
            row=m.evaluate(self.data,self.d,m.DEFAULTS)['analysts'][1]
            self.assertEqual((row['bull'],row['bear']),(b,s))
    def test_dates_and_validation(self):
        self.assertEqual(m.parse_date('115/11/12'),self.d)
        self.assertEqual(m.normalize([('2026-11-13',5),('2026-11-12',4)],self.d),[(self.d,4)])
        self.assertEqual(m.number('98.17%')/100,.9817)
        with self.assertRaises(ValueError):m.number('nan')
    def test_breadth_schema(self):
        obj={'tables':[{'fields':['類型','整體市場','股票'],'data':[['上漲(漲停)','7553(68)','277(12)'],['下跌(跌停)','7365(142)','730(10)']]}]}
        self.assertEqual(m.parse_breadth(obj),dict(advances=277,declines=730))
    def test_taifex_adapter(self):
        old=m.request
        m.request=lambda *a,**kw:'[{"Date":"20261112","PutCallVolumeRatio%":"98.17"}]'
        try:self.assertEqual(m.pcr(self.d,None),[(self.d,.9817)])
        finally:m.request=old

if __name__=='__main__':unittest.main(verbosity=2)
