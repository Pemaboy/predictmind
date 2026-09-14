import os, json, math, hashlib, hmac, secrets, sqlite3, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone

PORT = int(os.environ.get('PORT','8000'))
DB = os.environ.get('DATABASE_PATH','predictmind.db')
API_BASE = os.environ.get('FOOTBALL_API_BASE_URL','https://v3.football.api-sports.io')
API_KEY = os.environ.get('FOOTBALL_API_KEY','').strip()
PAYSTACK_SECRET = os.environ.get('PAYSTACK_SECRET_KEY','').strip()
APP_URL = os.environ.get('APP_URL','').strip()

PLANS = {
    'weekly': {'name':'Weekly AI Picks','price':5000,'days':7,'features':['Weekly AI match picks','1X2, Over 2.5 & BTTS','Estimated scores','Match confidence']},
    'monthly': {'name':'Monthly Pro','price':10000,'days':30,'features':['Everything in Weekly','Daily fixture analysis','Form & team-strength insights','Priority predictions']},
    'vip': {'name':'Monthly VIP','price':15000,'days':30,'features':['Everything in Pro','VIP high-confidence picks','Extended match reasoning','Early access to weekly report','VIP-only dashboard']}
}

DEMO = [
 ('Arsenal','Chelsea','England Premier League'),('Liverpool','Manchester United','England Premier League'),
 ('Manchester City','Tottenham','England Premier League'),('Bayern Munich','Dortmund','Germany Bundesliga'),
 ('Barcelona','Atletico Madrid','Spain La Liga'),('Real Madrid','Sevilla','Spain La Liga'),
 ('Inter','AC Milan','Italy Serie A'),('Napoli','Juventus','Italy Serie A'),
 ('PSG','Lyon','France Ligue 1'),('Ajax','PSV','Netherlands Eredivisie'),
 ('Galatasaray','Fenerbahce','Turkey Super Lig'),('Porto','Benfica','Portugal Primeira Liga')]

# Demo strengths used when the live provider is not connected.
STRENGTH = {
 'Arsenal':(1.75,0.85),'Chelsea':(1.35,1.05),'Liverpool':(1.9,0.8),'Manchester United':(1.35,1.25),
 'Manchester City':(2.0,0.75),'Tottenham':(1.5,1.3),'Bayern Munich':(2.0,0.85),'Dortmund':(1.55,1.25),
 'Barcelona':(1.9,0.85),'Atletico Madrid':(1.35,0.8),'Real Madrid':(2.0,0.8),'Sevilla':(1.1,1.35),
 'Inter':(1.65,0.75),'AC Milan':(1.4,1.0),'Napoli':(1.45,1.0),'Juventus':(1.25,0.85),
 'PSG':(1.95,0.9),'Lyon':(1.25,1.3),'Ajax':(1.65,1.0),'PSV':(1.7,0.95),
 'Galatasaray':(1.65,0.95),'Fenerbahce':(1.55,1.0),'Porto':(1.55,0.9),'Benfica':(1.6,0.95)
}

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, salt TEXT, pw TEXT, plan TEXT, expires TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fixtures(id INTEGER PRIMARY KEY, home TEXT, away TEXT, league TEXT, kickoff TEXT, external_id INTEGER)''')
    c.commit(); return c

def seed():
    c=db(); n=c.execute('SELECT COUNT(*) FROM fixtures').fetchone()[0]
    if n==0:
        now=datetime.now(timezone.utc)
        for i,(h,a,l) in enumerate(DEMO,1):
            c.execute('INSERT INTO fixtures VALUES(?,?,?,?,?,?)',(i,h,a,l,(now+timedelta(days=i%7,hours=i)).isoformat(),None))
        c.commit()
    c.close()
seed()

def hashpw(password,salt): return hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),180000).hex()
def newpw(password):
    salt=secrets.token_hex(16); return salt,hashpw(password,salt)
def user_by_token(c,token):
    if not token:return None
    try:
        uid=int(token)
        return c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    except:return None

def active_plan(u):
    if not u or not u['plan'] or not u['expires']: return None
    try:
        if datetime.fromisoformat(u['expires']) > datetime.now(timezone.utc): return u['plan']
    except: pass
    return None

def poisson(l,k): return math.exp(-l)*(l**k)/math.factorial(k)
def prediction(home,away):
    ha,hd=STRENGTH.get(home,(1.35,1.05)); aa,ad=STRENGTH.get(away,(1.25,1.1))
    lh=max(.15,min(4.2,ha*(1/ad)*1.08)); la=max(.15,min(4.0,aa*(1/hd)*.92))
    matrix={(x,y):poisson(lh,x)*poisson(la,y) for x in range(8) for y in range(8)}
    p_home=sum(v for (x,y),v in matrix.items() if x>y); p_draw=sum(v for (x,y),v in matrix.items() if x==y); p_away=sum(v for (x,y),v in matrix.items() if x<y)
    over=1-sum(v for (x,y),v in matrix.items() if x+y<=2)
    btts=1-math.exp(-lh)-math.exp(-la)+math.exp(-(lh+la))
    score=max(matrix,key=matrix.get)
    return {'home':round(p_home*100,1),'draw':round(p_draw*100,1),'away':round(p_away*100,1),'over25':round(over*100,1),'btts':round(btts*100,1),'score':f'{score[0]}-{score[1]}','confidence':round(max(p_home,p_draw,p_away)*100,1)}

def api_get(path,params=None):
    if not API_KEY:return None
    url=API_BASE.rstrip('/')+path
    if params:url += '?' + urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={'x-apisports-key':API_KEY,'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=15) as r:return json.loads(r.read().decode())

def sync_live():
    if not API_KEY:return {'ok':False,'message':'Live API key is not configured'}
    data=api_get('/fixtures',{'league':39,'season':datetime.now().year,'next':30})
    if not data:return {'ok':False,'message':'Could not reach football data provider'}
    c=db(); count=0
    for x in data.get('response',[]):
        f=x.get('fixture',{}); teams=x.get('teams',{}); league=x.get('league',{})
        eid=f.get('id'); h=teams.get('home',{}).get('name'); a=teams.get('away',{}).get('name')
        if not eid or not h or not a:continue
        c.execute('INSERT OR REPLACE INTO fixtures(id,home,away,league,kickoff,external_id) VALUES(COALESCE((SELECT id FROM fixtures WHERE external_id=?),?),?,?,?,?,?)',(eid,100000+eid,h,a,league.get('name','Football'),f.get('date',''),eid)); count+=1
    c.commit(); c.close(); return {'ok':True,'message':f'Synced {count} live fixtures'}

HTML = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>PredictMind AI</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:#07111f;color:#eef5ff}header{padding:18px 16px;background:#0b1b30;position:sticky;top:0;z-index:2;border-bottom:1px solid #18314e}.brand{font-size:23px;font-weight:800}.sub{color:#8fa7c0;font-size:12px}.wrap{max-width:900px;margin:auto;padding:16px}.hero{padding:24px 0}.hero h1{font-size:34px;margin:0 0 10px}.hero p{color:#a9bad0;line-height:1.5}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.card{background:#0c1b2e;border:1px solid #1c3855;border-radius:16px;padding:16px;margin-bottom:12px}.plan{min-height:250px}.price{font-size:28px;font-weight:800;margin:10px 0}.small{font-size:12px;color:#91a9c1}.btn{border:0;border-radius:10px;padding:11px 14px;font-weight:700;cursor:pointer;background:#20c77a;color:#06150e}.btn.alt{background:#173250;color:#fff}.btn.vip{background:#e0a72d;color:#1a1100}.row{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}.input{width:100%;padding:12px;border-radius:10px;border:1px solid #27445f;background:#081525;color:white;margin:5px 0}.fixture{padding:13px;border-top:1px solid #1b344d}.pill{padding:5px 8px;border-radius:20px;background:#173250;font-size:11px}.result{background:#071827;border-radius:12px;padding:12px;margin-top:8px}.bars div{margin:6px 0}.bar{height:8px;background:#1d3650;border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;background:#20c77a}.hide{display:none}@media(max-width:700px){.grid{grid-template-columns:1fr}.hero h1{font-size:28px}}
</style></head><body><header><div class="wrap"><div class="brand">🧠 PredictMind AI</div><div class="sub">Data-driven football intelligence</div></div></header><main class="wrap">
<section class="hero"><h1>AI Football Predictions</h1><p>Analyze fixtures with probabilities, estimated scores and confidence. Subscribe to unlock premium weekly and VIP reports.</p><div id="status" class="small">Connecting...</div></section>
<section class="card"><h2>Choose your plan</h2><div class="grid"><div class="card plan"><h3>Weekly AI Picks</h3><div class="price">₦5,000 <span class="small">/ 7 days</span></div><p>Weekly AI match picks, 1X2, Over 2.5, BTTS, estimated scores and confidence.</p><button class="btn" onclick="pay('weekly')">Get Weekly</button></div><div class="card plan"><h3>Monthly Pro</h3><div class="price">₦10,000 <span class="small">/ month</span></div><p>Daily fixture analysis, form insights and priority predictions.</p><button class="btn" onclick="pay('monthly')">Get Monthly</button></div><div class="card plan"><h3>Monthly VIP</h3><div class="price">₦15,000 <span class="small">/ month</span></div><p>VIP high-confidence picks, extended reasoning and early weekly-report access.</p><button class="btn vip" onclick="pay('vip')">Get VIP</button></div></div></section>
<section class="card"><h2>Account</h2><div id="account"><input id="name" class="input" placeholder="Name"><input id="email" class="input" placeholder="Email"><input id="pw" class="input" type="password" placeholder="Password"><div class="row"><button class="btn" onclick="register()">Create account</button><button class="btn alt" onclick="login()">Login</button></div></div><div id="me" class="hide"></div></section>
<section class="card"><div class="row"><h2>Weekly AI Report</h2><button class="btn alt" onclick="weekly()">Generate</button></div><div id="report" class="small">Your weekly report will appear here after you subscribe.</div></section>
<section class="card"><div class="row"><h2>Fixtures</h2><button class="btn alt" onclick="load()">Refresh</button></div><div id="fixtures">Loading fixtures...</div></section>
</main><script>
let token=localStorage.getItem('pm_token')||'';const $=x=>document.getElementById(x);async function api(u,o={}){o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});if(token)o.headers.Authorization='Bearer '+token;let r=await fetch(u,o);let d=await r.json();if(!r.ok)throw Error(d.detail||d.message||'Request failed');return d}
async function register(){try{let d=await api('/api/register',{method:'POST',body:JSON.stringify({name:$('name').value,email:$('email').value,password:$('pw').value})});token=d.token;localStorage.setItem('pm_token',token);me();alert('Account created successfully');}catch(e){alert(e.message)}}
async function login(){try{let d=await api('/api/login',{method:'POST',body:JSON.stringify({email:$('email').value,password:$('pw').value})});token=d.token;localStorage.setItem('pm_token',token);me();alert('Login successful');}catch(e){alert(e.message)}}
async function me(){if(!token)return;try{let d=await api('/api/me');$('account').className='hide';$('me').className='';$('me').innerHTML='<b>'+d.name+'</b> · '+d.email+'<br>Plan: <b>'+ (d.plan||'Free') +'</b> '+(d.expires?'until '+new Date(d.expires).toLocaleDateString():'')+' <button class="btn alt" onclick="logout()">Logout</button>'}catch(e){logout()}}
function logout(){token='';localStorage.removeItem('pm_token');location.reload()}
async function load(){try{let d=await api('/api/fixtures');$('fixtures').innerHTML=d.fixtures.map(f=>`<div class="fixture"><div class="row"><b>${f.home} vs ${f.away}</b><span class="pill">${f.league}</span></div><div class="small">${new Date(f.kickoff).toLocaleString()} <button class="btn" onclick="pred(${f.id})">AI Analyze</button></div><div id="p${f.id}"></div></div>`).join('')}catch(e){$('fixtures').innerText=e.message}}
async function pred(id){try{let d=await api('/api/predict/'+id);$('p'+id).innerHTML='<div class="result"><b>AI Prediction</b><div class="bars"><div>Home '+d.home+'%<div class="bar"><i style="width:'+d.home+'%"></i></div></div><div>Draw '+d.draw+'%<div class="bar"><i style="width:'+d.draw+'%"></i></div></div><div>Away '+d.away+'%<div class="bar"><i style="width:'+d.away+'%"></i></div></div></div><div>Over 2.5: <b>'+d.over25+'%</b> · BTTS: <b>'+d.btts+'%</b> · Score: <b>'+d.score+'</b></div><div class="small">Confidence: '+d.confidence+'%. Probabilities are estimates, not guarantees.</div></div>'}catch(e){alert(e.message)}}
async function weekly(){try{let d=await api('/api/weekly');$('report').innerHTML=d.message+'<br><br>'+d.picks.map((x,i)=>`<b>${i+1}. ${x.match}</b> — ${x.pick} (${x.confidence}%)<br><span class="small">Score ${x.score} · O2.5 ${x.over25}% · BTTS ${x.btts}%</span><br>`).join('<br>')}catch(e){alert(e.message)}}
async function pay(plan){try{let d=await api('/api/pay',{method:'POST',body:JSON.stringify({plan})});if(d.authorization_url)location.href=d.authorization_url;else alert(d.message)}catch(e){alert(e.message)}}
(async()=>{try{let h=await fetch('/health');let d=await h.json();$('status').innerText=d.api?'🟢 Website online · Live football API connected':'🟢 Website online · Demo football data ready';}catch(e){$('status').innerText='🔴 Server connection problem'}await me();await load()})();
</script></body></html>'''

class H(BaseHTTPRequestHandler):
    def send(self,code,data,ctype='application/json'):
        b=data.encode() if isinstance(data,str) else data; self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def json(self,code,obj):self.send(code,json.dumps(obj),'application/json')
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or '{}')
    def token(self):return self.headers.get('Authorization','').replace('Bearer ','').strip()
    def user(self):
        c=db();u=user_by_token(c,self.token());c.close();return u
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path
        if path=='/' or path=='/index.html':return self.send(200,HTML,'text/html; charset=utf-8')
        if path=='/health':return self.json(200,{'ok':True,'api':bool(API_KEY)})
        if path=='/api/me':
            u=self.user()
            if not u:return self.json(401,{'detail':'Not logged in'})
            return self.json(200,{'name':u['name'],'email':u['email'],'plan':active_plan(u),'expires':u['expires']})
        if path=='/api/fixtures':
            c=db();rows=c.execute('SELECT * FROM fixtures ORDER BY kickoff LIMIT 50').fetchall();c.close();return self.json(200,{'fixtures':[dict(x) for x in rows]})
        if path.startswith('/api/predict/'):
            fid=int(path.rsplit('/',1)[1]);c=db();f=c.execute('SELECT * FROM fixtures WHERE id=?',(fid,)).fetchone();c.close()
            if not f:return self.json(404,{'detail':'Fixture not found'})
            return self.json(200,prediction(f['home'],f['away']))
        if path=='/api/weekly':
            u=self.user()
            if not active_plan(u):return self.json(403,{'detail':'Weekly report is for subscribers. Choose Weekly ₦5,000, Monthly ₦10,000 or VIP ₦15,000.'})
            c=db();rows=c.execute('SELECT * FROM fixtures ORDER BY kickoff LIMIT 10').fetchall();c.close();p=[]
            for f in rows:
                x=prediction(f['home'],f['away']); choices=[('Home Win',x['home']),('Draw',x['draw']),('Away Win',x['away'])];pick=max(choices,key=lambda z:z[1]);p.append({'match':f['home']+' vs '+f['away'],'pick':pick[0],'confidence':pick[1],'score':x['score'],'over25':x['over25'],'btts':x['btts']})
            p.sort(key=lambda z:z['confidence'],reverse=True);return self.json(200,{'message':'This week\'s AI report — top confidence matches','picks':p[:7]})
        if path.startswith('/api/paystack/callback'):
            return self.send(200,'Payment received. You can return to PredictMind AI.','text/plain')
        return self.json(404,{'detail':'Not found'})
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        try:data=self.body()
        except:return self.json(400,{'detail':'Invalid JSON'})
        if path=='/api/register':
            name,email,pw=(data.get('name','').strip(),data.get('email','').strip().lower(),data.get('password',''))
            if not name or '@' not in email or len(pw)<6:return self.json(400,{'detail':'Enter a name, valid email and password of at least 6 characters.'})
            salt,ph=newpw(pw);c=db()
            try:c.execute('INSERT INTO users(email,name,salt,pw,plan,expires) VALUES(?,?,?,?,?,?)',(email,name,salt,ph,'',''));c.commit();uid=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()['id'];c.close();return self.json(200,{'token':str(uid)})
            except sqlite3.IntegrityError:c.close();return self.json(400,{'detail':'Account already exists. Login instead.'})
        if path=='/api/login':
            email,pw=data.get('email','').strip().lower(),data.get('password','');c=db();u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone();ok=u and hmac.compare_digest(hashpw(pw,u['salt']),u['pw']);c.close()
            if not ok:return self.json(401,{'detail':'Incorrect email or password.'})
            return self.json(200,{'token':str(u['id'])})
        if path=='/api/sync':return self.json(200,sync_live())
        if path=='/api/pay':
            u=self.user();plan=data.get('plan');
            if not u:return self.json(401,{'detail':'Create an account or login first.'})
            if plan not in PLANS:return self.json(400,{'detail':'Invalid plan'})
            if not PAYSTACK_SECRET:return self.json(200,{'message':f'{PLANS[plan]["name"]} selected at ₦{PLANS[plan]["price"]:,}. Paystack is not connected yet. Add PAYSTACK_SECRET_KEY in Render to activate online payments.'})
            email=u['email']; amount=PLANS[plan]['price']*100; callback=(APP_URL.rstrip('/') if APP_URL else 'http://localhost:'+str(PORT))+'/api/paystack/callback'
            payload=json.dumps({'email':email,'amount':amount,'currency':'NGN','callback_url':callback,'metadata':{'user_id':u['id'],'plan':plan}}).encode();req=urllib.request.Request('https://api.paystack.co/transaction/initialize',data=payload,headers={'Authorization':'Bearer '+PAYSTACK_SECRET,'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=15) as r:res=json.loads(r.read().decode())
                if res.get('status'):return self.json(200,{'authorization_url':res['data']['authorization_url']})
                return self.json(400,{'detail':'Paystack could not initialize payment.'})
            except Exception as e:return self.json(400,{'detail':'Paystack connection error. Check the secret key.'})
        return self.json(404,{'detail':'Not found'})

if __name__=='__main__':
    print('PredictMind AI running on port',PORT)
    ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
