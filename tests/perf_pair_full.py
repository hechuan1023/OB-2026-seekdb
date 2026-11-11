import argparse,array,json,math,random,statistics,subprocess,time,uuid
from pathlib import Path

parser=argparse.ArgumentParser(); parser.add_argument('--image',required=True); parser.add_argument('--compare',required=True); parser.add_argument('--output',required=True); parser.add_argument('--rounds',type=int,default=3); parser.add_argument('--groups',type=int,default=5); parser.add_argument('--queries',type=int,default=200);args=parser.parse_args()
if args.groups < 1 or args.queries < 2: parser.error('groups must be positive and queries at least 2')
rng=random.Random(20261110); n=10000; dims=128; count=args.queries
data=[(i,'g'+str(i%args.groups),list(array.array('f',(rng.uniform(-1,1) for _ in range(dims))))) for i in range(n)]
queries=[]
for i in range(count):
 q=list(array.array('f',(rng.uniform(-1,1) for _ in range(dims)))); tag='g'+str((i//2)%args.groups) if i%2 else None
 truth=sorted(((math.dist(v,q),pk) for pk,t,v in data if tag is None or t==tag))[:10]
 queries.append((q,tag,truth))
rounds=[]
for k in range(args.rounds * 2):
 image = args.image if k % 2 == 0 else args.compare
 vol='mvdb-jsonbench-'+uuid.uuid4().hex[:12];subprocess.run(['docker','volume','create',vol],check=True,capture_output=True)
 name=vol+'-db'
 p=subprocess.Popen(['docker','run','--rm','-i','--name',name,'--network','none','--cpus=2','--memory=256m','--memory-swap=256m','-v',vol+':/data',image],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',bufsize=1)
 try:
  assert json.loads(p.stdout.readline())=={'type':'ready'}
  seq=[0]
  def send(sql):
   seq[0]+=1;p.stdin.write(json.dumps({'id':seq[0],'op':'execute','sql':sql,'search_mode':'exact'},separators=(',',':'))+'\n');p.stdin.flush()
   result=json.loads(p.stdout.readline());assert result['id']==seq[0] and result['ok'],result
   return result
  send('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(128))')
  t=time.perf_counter()
  for start in range(0,n,100):
   sql='INSERT INTO t VALUES '+','.join("(%d,'%s','%s')"%(pk,tag,json.dumps(v,separators=(',',':'))) for pk,tag,v in data[start:start+100]);assert send(sql)['affected_rows']==100
  writing=time.perf_counter()-t
  send('CREATE VECTOR INDEX vi ON t(v)')
  stats={'filtered':[],'unfiltered':[]}; first={}
  for q,tag,truth in queries:
   where='' if tag is None else " WHERE tag='"+tag+"'"
   sql="SELECT id,tag,v,L2_DISTANCE(v,'"+json.dumps(q,separators=(',',':'))+"') AS d FROM t"+where+' ORDER BY d LIMIT 10'
   t=time.perf_counter(); rows=send(sql)['rows'];dt=time.perf_counter()-t
   assert [row[0] for row in rows]==[pk for dist,pk in truth]
   assert all(row[1] == 'g'+str(pk%args.groups) and row[2] == data[pk][2] for row,(dist,pk) in zip(rows,truth))
   assert all(math.isclose(row[3],dist,rel_tol=1e-12,abs_tol=1e-12) for row,(dist,_) in zip(rows,truth))
   group='filtered' if tag else 'unfiltered';stats[group].append(dt)
   if tag is not None and tag not in first:first[tag]=dt
  metrics=subprocess.check_output(['docker','exec',name,'sh','-c','sync; cat /sys/fs/cgroup/memory.peak; echo IO; cat /sys/fs/cgroup/io.stat'],text=True,encoding='utf-8')
  seq[0]+=1;p.stdin.write(json.dumps({'id':seq[0],'op':'shutdown'})+'\n');p.stdin.flush();assert json.loads(p.stdout.readline())['ok'];assert p.wait(timeout=30)==0
  record={'image':image,'round':k//2+1,'write_seconds':writing,'query_seconds':sum(sum(x) for x in stats.values()),'filtered_seconds':sum(stats['filtered']),'unfiltered_seconds':sum(stats['unfiltered']),'first_filter_seconds':first,'recall_exact':1.0,'memory_peak_bytes':int(metrics.splitlines()[0]),'io_stat_raw':'\n'.join(metrics.splitlines()[2:])};rounds.append(record);print(json.dumps(record),flush=True)
 finally:
  if p.poll() is None:p.kill();p.wait()
  p.stdin.close();p.stdout.close();p.stderr.close();subprocess.run(['docker','volume','rm',vol],capture_output=True,check=True)
Path(args.output).write_text(json.dumps({'images':[args.image,args.compare],'seed':20261110,'rows':n,'dimensions':dims,'queries':count,'groups':args.groups,'rounds':rounds,'notes':'Local JSONL correctness and wall-clock comparison; not official benchmark. Queries and truth prepared on host, data volumes independent, no cold-cache control. cgroup metrics read after sync before shutdown, exec helper included; io.stat is raw, no device attribution verified.'},indent=2)+'\n',encoding='utf-8')
