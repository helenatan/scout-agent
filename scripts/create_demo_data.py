"""Create fictional demo records; refuse to replace an existing research database."""
import datetime as dt
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'m0/data/pai_dump_2026-09-06.sqlite'
path.parent.mkdir(parents=True,exist_ok=True)
if path.exists():raise SystemExit('Database already exists; left unchanged.')
con=sqlite3.connect(path)
con.executescript('''
CREATE TABLE job_postings_raw(id TEXT PRIMARY KEY,source TEXT,source_id TEXT,company TEXT,title TEXT,location TEXT,run_date TEXT,last_seen_date TEXT,is_us INTEGER,posted_date TEXT,url TEXT,description_text TEXT);
CREATE TABLE job_postings_enriched(raw_id TEXT,pipeline_version TEXT,company_normalized TEXT,normalized_title TEXT);
CREATE TABLE fetch_log(source TEXT,run_date TEXT,status TEXT,records_fetched INTEGER,records_inserted INTEGER,error_message TEXT);
''')
for i,(source,title) in enumerate([('greenhouse','Product Manager, Agent Tools'),('jsearch','Product Manager, Agent Tools'),('ashby','Senior Product Manager, Analytics')],1):
    pid=f'00000000-0000-4000-8000-{i:012d}'
    description='About the role\nBuild useful products at fictional Example Labs.\nQualifications\nExperience building agent products is preferred.\nBenefits\nFlexible hours.'
    con.execute('INSERT INTO job_postings_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(pid,source,str(i),'Example Labs',title,'Remote','2026-08-10','2026-08-10',1,'2026-08-10','https://example.com/jobs/'+str(i),description))
    con.execute('INSERT INTO job_postings_enriched VALUES (?,?,?,?)',(pid,'v1.0','example labs','Product Manager'))
for day in range(31):
    date=(dt.date(2026,8,1)+dt.timedelta(days=day)).isoformat()
    for source in ('greenhouse','jsearch','ashby'):
        n=int(date=='2026-08-10')
        con.execute('INSERT INTO fetch_log VALUES (?,?,?,?,?,?)',(source,date,'ok',n,n,None))
con.commit();con.close()
print('Created fictional demonstration data at',path)
