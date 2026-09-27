#property strict
#property copyright "skyphusion"
#property description "File mailbox for straightedge MT4 adapter. FILE_COMMON."

input int Slippage = 30;
input int ReconcileMagic = 0;   // 0 = report every position that has no stop
input int SingletonStaleSeconds = 15;  // age at which a crashed instance's claim is taken over
input int MailboxStaleSeconds = 60;    // age at which a wedged mailbox mutex is taken over
input int ClaimOpenRetries = 10;       // attempts to open a claim this instance already owns
input int ClaimOpenRetryMs = 20;       // wait between those attempts

// The three retry ladders, as NAMED constants rather than literals buried in the
// loops. They are named because the desk has to know them: its send budget must
// be strictly longer than everything this Expert can spend before it is able to
// reply, or the desk gives up on a request that is still executing and the
// operator is told an order failed while it is on its way to the broker. The
// ping reply declares the arithmetic below, the desk checks it on every connect,
// and `tests/test_send_budget.py` cross-checks that the loops really are driven
// by these constants. A literal in a loop plus a number in a reply is two copies
// that drift in silence.
#define SE_SEND_TRIES      8
#define SE_MODIFY_TRIES    5
#define SE_ROLLBACK_TRIES  6
#define SE_RETRY_SLEEP_MS  50
#define SE_LADDER_SLEEP_MS ((SE_SEND_TRIES + SE_MODIFY_TRIES + SE_ROLLBACK_TRIES) * SE_RETRY_SLEEP_MS)
#define SE_BROKER_CALLS    (SE_SEND_TRIES + SE_MODIFY_TRIES + SE_ROLLBACK_TRIES)
// SendRetry returns this when a send TIMED OUT and the book could not settle it.
// Distinct from -1, which is a clean rejection: -1 means nothing is on the book
// and -2 means we do not know. Not a count, so it stays out of the ladder
// arithmetic above, which is what the desk derives its send budget from.
#define SE_SEND_UNKNOWN    -2

// The stale-request fence. A request carries `ttl_ms`, the budget the desk is
// waiting; once that has elapsed the desk has ALREADY given up and reported a
// failure, so executing the request would fire a trade the operator was told had
// not happened. The window used to be unbounded across a desk process exit, and
// a silent restart was observed on the live box at 2026-09-26T01:56:49Z.
//
// FenceGraceSec absorbs the one-second resolution of a filesystem timestamp, in
// the direction that cannot kill a live request: a fresh request read a moment
// after it was written can compute an age one second too high, and refusing THAT
// would break the desk. The fence is about a request minutes old, so a second of
// slack costs it nothing.
input int FenceGraceSec = 2;

// Terminal-wide named locks. GlobalVariableSetOnCondition is the ONLY primitive
// MQL4 documents as atomic, and it documents this exact use: "Function provides
// atomic access to the global variable, so it can be used for providing of a
// mutex at interaction of several Expert Advisors working simultaneously within
// one client terminal." Both locks below are built on it and on nothing else.
// FileMove's atomicity is NOT documented, so it is used as a second barrier and
// never as the guarantee.
#define SE_SINGLETON_LOCK "straightedge_mt4_singleton"
#define SE_MAILBOX_LOCK   "straightedge_mt4_mailbox"

bool gBusy = false;
bool gHoldsSingleton = false;
bool gHoldsMailbox = false;
string gClaimPath = "";

// File timestamps are compared against TimeLocal(), and MQL4 does not state
// whether FILE_MODIFY_DATE comes back in local time or in UTC. Guessing is not
// acceptable here: guess wrong in one direction and every request looks ancient
// (the desk stops working), guess wrong in the other and a request from hours ago
// looks FRESH (the fence silently stops existing). So the offset is MEASURED at
// init against a file this Expert writes itself, one line below the guess it
// replaces. gFileTimeKnown false means it could not be measured, and an
// unmeasurable age refuses a SEND rather than passing it.
bool gFileTimeKnown = false;
int  gFileTimeOffset = 0;

// A lock is held as a TIMESTAMP that the holder refreshes. A lock whose stamp
// has not moved for staleSecs is taken over with a compare-and-set against the
// exact stale value, so if several instances see it stale at the same moment
// only one wins the swap. GlobalVariableTemp creates the variable; temporary
// globals "exist only while the client terminal is running", so a terminal crash
// cannot leave one behind on disk to block a legitimate restart. Its initial
// value is undocumented, which is why a non-zero fresh variable still falls
// through to the stale path instead of being assumed free.
bool LockAcquire(string name, int staleSecs)
{
   double now = (double)TimeLocal();
   if(now <= 0)
      now = 1;
   // Create it if absent. If creation fails AND it still does not exist, refuse
   // rather than fall through and treat an unreadable lock as a free one.
   if(!GlobalVariableCheck(name) && !GlobalVariableTemp(name) && !GlobalVariableCheck(name))
      return false;
   if(GlobalVariableSetOnCondition(name, now, 0.0))
      return true;
   double held = GlobalVariableGet(name);
   if(held == 0.0)
      return GlobalVariableSetOnCondition(name, now, 0.0);
   if(now - held > staleSecs)
      return GlobalVariableSetOnCondition(name, now, held);
   return false;
}

void LockRefresh(string name)
{
   GlobalVariableSet(name, (double)TimeLocal());
}

void LockRelease(string name)
{
   GlobalVariableSet(name, 0.0);
}

double LockAge(string name)
{
   double held = GlobalVariableGet(name);
   if(held == 0.0)
      return 0;
   return (double)TimeLocal() - held;
}

// Measure how this terminal reports file timestamps, by writing one and reading
// it back. Returns true when the offset is known. The probe file is removed
// again; if it cannot be written or read, the fence says so rather than assuming.
bool CalibrateFileTime()
{
   string probe = "mt4_risk_bot.timeprobe";
   int h = FileOpen(probe, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
   {
      Print("mt4riskbot file-time calibration FAILED to write ", probe,
            " err=", GetLastError(),
            ". Stale-request ages cannot be measured, so SEND requests whose age",
            " is unknown will be REFUSED. Fix the Common Files permissions.");
      return false;
   }
   FileWriteString(h, "probe\n");
   FileFlush(h);
   FileClose(h);
   int now = (int)TimeLocal();
   int stamp = (int)FileGetInteger(probe, FILE_MODIFY_DATE, true);
   FileDelete(probe, FILE_COMMON);
   if(stamp <= 0)
   {
      Print("mt4riskbot file-time calibration FAILED to read a modify date",
            " err=", GetLastError(),
            ". SEND requests whose age is unknown will be REFUSED.");
      return false;
   }
   gFileTimeOffset = now - stamp;
   Print("mt4riskbot file-time calibration ok: offset ", gFileTimeOffset,
         "s between TimeLocal() and FILE_MODIFY_DATE. Stale requests will be",
         " refused using this offset.");
   return true;
}

// The age of a request file, in seconds, or -1 when it cannot be measured.
// Measured against a stamp taken from the filesystem that HOLDS the file, so the
// desk's own clock never enters this arithmetic: the desk and the terminal can be
// on different hosts and a wall-clock deadline crossing that boundary would make
// a stale request look fresh whenever the terminal ran behind.
int RequestAgeSec(int stamp)
{
   if(!gFileTimeKnown || stamp <= 0)
      return -1;
   int age = (int)TimeLocal() - (stamp + gFileTimeOffset);
   if(age < 0)
      return -1;
   return age;
}

// The ops that can change the book. An unmeasurable age refuses these and allows
// the rest: a stale `tick` is harmless and refusing reads would take the desk
// down over a permissions problem, while a stale `market` is a trade nobody wants.
bool IsSendOp(string op)
{
   return op == "market" || op == "working" || op == "modify_position"
       || op == "modify_working" || op == "cancel" || op == "close"
       || op == "close_by";
}

// Is this request past the budget the desk said it was waiting?
// `ttl_ms` absent (an older desk) means no fence and the request is executed, as
// it always was; the desk's own withdrawal is the layer that covers that case.
bool RequestExpired(string body, int stamp, int &ageOut, int &ttlOut)
{
   ttlOut = (int)StringToInteger(KV(body, "ttl_ms"));
   ageOut = RequestAgeSec(stamp);
   if(ttlOut <= 0)
      return false;
   int ttlSec = (ttlOut + 999) / 1000 + FenceGraceSec;
   if(ageOut < 0)
      return IsSendOp(KV(body, "op"));
   return ageOut > ttlSec;
}

// Report every position that is open with no stop loss. A position that
// predates this session is NOT adopted: this Expert only reports it, so a
// human decides. Silence here is how an orphan from a previous session
// becomes permanent.
void ReportUnmanaged()
{
   int total = OrdersTotal();
   int found = 0;
   for(int i=0; i<total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderType() > OP_SELL)
         continue;
      if(ReconcileMagic != 0 && OrderMagicNumber() != ReconcileMagic)
         continue;
      if(OrderStopLoss() != 0)
         continue;
      found++;
      Print("mt4riskbot UNMANAGED ticket=", OrderTicket(),
            " symbol=", OrderSymbol(),
            " type=", OrderType(),
            " lots=", DoubleToString(OrderLots(), 2),
            " magic=", OrderMagicNumber(),
            " opened=", TimeToString(OrderOpenTime()),
            " -- open with NO STOP, not adopted by this session");
   }
   if(found > 0)
      Print("mt4riskbot ", found,
            " position(s) open with NO STOP at startup. Close or protect them in the terminal.");
}

int OnInit()
{
   gFileTimeKnown = CalibrateFileTime();
   // LAYER 2, visibility. Exactly one straightedge Expert per terminal. A second
   // instance refuses to initialise rather than quietly competing for the
   // mailbox, because two instances would both send the same order.
   if(!LockAcquire(SE_SINGLETON_LOCK, SingletonStaleSeconds))
   {
      Print("mt4riskbot REFUSING TO START: another straightedge Expert is already",
            " running in this terminal and owns the mt4_risk_bot mailbox (holder",
            " last seen ", DoubleToString(LockAge(SE_SINGLETON_LOCK), 0), "s ago).",
            " Attach this Expert to exactly ONE chart. Two instances would both",
            " send the same order, so this one is stopping.");
      return(INIT_FAILED);
   }
   gHoldsSingleton = true;
   // Per-instance claim path. Only this chart ever writes it, so an orphan left
   // by a crash is this chart's own to overwrite on its next claim.
   gClaimPath = "mt4_risk_bot.req.claim." + IntegerToString((int)ChartID());
   if(!EventSetMillisecondTimer(100))
      EventSetTimer(1);
   Print("mt4riskbot singleton acquired chart=", IntegerToString((int)ChartID()),
         " claim=", gClaimPath);
   ReportUnmanaged();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   // Release ONLY what this instance actually holds. OnDeinit also runs after
   // OnInit returns INIT_FAILED, and a refusing instance that released the lock
   // would hand the mailbox to itself on the next attach.
   if(gHoldsMailbox)
   {
      LockRelease(SE_MAILBOX_LOCK);
      gHoldsMailbox = false;
   }
   if(gHoldsSingleton)
   {
      LockRelease(SE_SINGLETON_LOCK);
      gHoldsSingleton = false;
   }
}

void OnTimer()
{
   Process();
}

void OnTick()
{
   Process();
}

void Process()
{
   // Heartbeat. Refreshed from the timer AND from ticks, so a terminal where
   // EventSetMillisecondTimer failed still keeps this instance's claim fresh.
   if(gHoldsSingleton)
      LockRefresh(SE_SINGLETON_LOCK);
   if(gBusy)
      return;
   if(!FileIsExist("mt4_risk_bot.req", FILE_COMMON))
      return;
   gBusy = true;

   // LAYER 1a, correctness. The documented atomic mutex. Nothing below this line
   // runs in two instances of this Expert in the same terminal at once.
   if(!LockAcquire(SE_MAILBOX_LOCK, MailboxStaleSeconds))
   {
      gBusy = false;
      return;
   }
   gHoldsMailbox = true;

   // LAYER 1b, correctness. Claim by rename. The shared name is GONE before a
   // single byte of the body is read, and the body is read from a path only this
   // instance writes. A loser's FileMove fails because the source no longer
   // exists, and it returns without reading a request another instance is about
   // to execute. The old sequence read the shared name FIRST and deleted it
   // afterwards, so both readers got the whole body and both sent the order.
   // The stamp is taken from the SHARED name, BEFORE the rename, because a
   // rename's effect on a modification time is not documented and a rename that
   // refreshed it would make every request look new -- the fence would pass
   // everything while appearing to work. Read again after the claim and the OLDER
   // of the two is used (below), so a file swapped in between these two reads
   // cannot make an old request look young.
   int reqStamp = (int)FileGetInteger("mt4_risk_bot.req", FILE_MODIFY_DATE, true);
   if(!FileMove("mt4_risk_bot.req", FILE_COMMON, gClaimPath, FILE_COMMON|FILE_REWRITE))
   {
      Print("mt4riskbot claim lost: mt4_risk_bot.req was gone before this instance",
            " could rename it err=", GetLastError(), ". Nothing executed. If this",
            " repeats, a second Expert is claiming the mailbox.");
      LockRelease(SE_MAILBOX_LOCK);
      gHoldsMailbox = false;
      gBusy = false;
      return;
   }

   // LAYER 1c, liveness. A TRANSIENT FileOpen failure on a claim this instance
   // already owns is not a reason to destroy the request. Measured on the live
   // desk on 2026-09-25: 84 requests over 10.8h were claimed by rename and then
   // dropped right here with err=5004 (ERR_CANNOT_OPEN_FILE) on the very next
   // statement, and each one cost the adapter a full bridge timeout. That is
   // what every "mt4 bridge timeout" in that journal actually was.
   //
   // Retrying is safe for a specific reason, not an optimistic one: the claim
   // path is private to this chart, SE_MAILBOX_LOCK is held, and NOTHING HAS
   // BEEN EXECUTED YET, so a second open cannot duplicate an order. The desk
   // already applies this same discipline on its own side (_retry_unlink and
   // _atomic_write in mt4_live.py spin on PermissionError until their
   // deadline), so this closes an asymmetry rather than inventing a policy.
   //
   // The budget is bounded against the ADAPTER's budget rather than guessed:
   // mt4.timeout_ms is 5000ms and a measured steady-state round trip on this
   // rig is about 205ms, so 10 attempts x 20ms is at most 180ms of added wait,
   // under 4% of the adapter's budget and well inside one round trip.
   //
   // After the last attempt the request is STILL dropped and STILL logged, and
   // a RECOVERY is logged too, so the rate of transient failures stays visible
   // instead of being hidden by the retry that fixes it.
   int share = FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE;
   int tries = (ClaimOpenRetries < 1) ? 1 : ClaimOpenRetries;
   int h = INVALID_HANDLE;
   int attempts = 0;
   int lastErr = 0;
   while(attempts < tries)
   {
      ResetLastError();
      h = FileOpen(gClaimPath, share);
      attempts = attempts + 1;
      if(h != INVALID_HANDLE)
         break;
      lastErr = GetLastError();
      if(attempts < tries)
         Sleep(ClaimOpenRetryMs);
   }
   if(h == INVALID_HANDLE)
   {
      // Claimed and unreadable after every attempt. The request is DROPPED,
      // never put back: the adapter times out and reports no result, which is
      // the safe answer. A request that is replayed after a restart is a
      // duplicate order.
      Print("mt4riskbot claimed request unreadable path=", gClaimPath,
            " err=", lastErr, " attempts=", attempts, ". Dropped, not replayed.");
      FileDelete(gClaimPath, FILE_COMMON);
      LockRelease(SE_MAILBOX_LOCK);
      gHoldsMailbox = false;
      gBusy = false;
      return;
   }
   if(attempts > 1)
      Print("mt4riskbot claim open recovered path=", gClaimPath,
            " attempts=", attempts, " lastErr=", lastErr,
            ". The request was NOT lost.");
   int claimStamp = (int)FileGetInteger(gClaimPath, FILE_MODIFY_DATE, true);
   if(claimStamp > 0 && (reqStamp <= 0 || claimStamp < reqStamp))
      reqStamp = claimStamp;
   string body = "";
   while(!FileIsEnding(h))
      body = body + FileReadString(h) + "\n";
   FileClose(h);
   FileDelete(gClaimPath, FILE_COMMON);

   // LAYER 1d, the stale fence. Everything above this point is about executing
   // the request exactly once; this is about NOT executing one that nobody wants
   // any more. The desk removes an abandoned request itself, but it cannot do so
   // if the desk process died, and that is the case with no bound on it at all.
   int fenceAge = -1;
   int fenceTtl = 0;
   string reply;
   if(RequestExpired(body, reqStamp, fenceAge, fenceTtl))
   {
      string op = KV(body, "op");
      Print("mt4riskbot REFUSED a stale request op=", op,
            " id=", KV(body, "id"),
            " age=", fenceAge, "s ttl=", fenceTtl, "ms",
            " (age -1 means it could not be measured). The desk has already",
            " given up on this request and reported it as failed; executing it",
            " would open a trade nobody is expecting. Nothing was sent.");
      reply = Fail(KV(body, "id"), 4109, "request_expired")
            + "survivor_ticket=0\n"
            + "age_sec=" + IntegerToString(fenceAge) + "\n";
   }
   else
      reply = Handle(body);
   // LAYER 1e, DELIVERY. The reply write gets the same retry as the claim read,
   // and for a sharper reason: by the time control reaches here, Handle() has
   // ALREADY EXECUTED the operation. A silent failure to deliver the reply is
   // therefore the worst failure this Expert can produce. For op=market it means
   // the order is LIVE while the adapter is told nothing, so the adapter times
   // out and writes the trade off. The previous code tested
   // `if(w != INVALID_HANDLE)` and simply fell through when the open failed,
   // writing no reply and logging NOTHING, so the fault was invisible in the
   // Experts log and could only be seen from outside.
   //
   // Measured, 2026-09-26, a 24.3 minute window from 01:57:15Z to 02:21:48Z with
   // the market CLOSED, watching the mailbox directory: the Expert claimed and
   // released all 2166 requests but created only 2163 `.res.tmp` files. The three
   // requests with no `.res.tmp` are exactly the three `reconnect` events in
   // journal.jsonl at 02:17:28, 02:20:34 and 02:21:15, each one adapter budget
   // after its request. The Experts log recorded none of it.
   //
   // 3/2166 is 0.139%. The claim-side rate on 2026-09-25 was 84 over roughly the
   // same request volume, about 0.149%. Two failure sites, one underlying
   // transient, whichever FileOpen happens to be in flight.
   int w = INVALID_HANDLE;
   int wAttempts = 0;
   int wErr = 0;
   while(wAttempts < tries)
   {
      ResetLastError();
      w = FileOpen("mt4_risk_bot.res.tmp", FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
      wAttempts = wAttempts + 1;
      if(w != INVALID_HANDLE)
         break;
      wErr = GetLastError();
      if(wAttempts < tries)
         Sleep(ClaimOpenRetryMs);
   }
   if(w != INVALID_HANDLE)
   {
      FileWriteString(w, reply);
      FileFlush(w);
      FileClose(w);
      FileDelete("mt4_risk_bot.res", FILE_COMMON);
      // The rename was previously a bare statement, so a failed publish looked
      // exactly like a successful one. It is the last step between an executed
      // operation and the adapter learning about it, so it is now checked.
      if(!FileMove("mt4_risk_bot.res.tmp", FILE_COMMON, "mt4_risk_bot.res", FILE_COMMON))
         Print("mt4riskbot REPLY NOT DELIVERED: the operation RAN but .res.tmp could",
               " not be renamed to .res err=", GetLastError(), ". The adapter will time",
               " out on an operation that ALREADY EXECUTED.");
      else if(wAttempts > 1)
         Print("mt4riskbot reply open recovered attempts=", wAttempts,
               " lastErr=", wErr, ". The reply WAS delivered.");
   }
   else
      Print("mt4riskbot REPLY NOT DELIVERED: the operation RAN but .res.tmp could not",
            " be opened after ", wAttempts, " attempts err=", wErr, ". The adapter will",
            " time out on an operation that ALREADY EXECUTED.");
   LockRelease(SE_MAILBOX_LOCK);
   gHoldsMailbox = false;
   gBusy = false;
}

string KV(string body, string key)
{
   string lines[];
   int n = StringSplit(body, StringGetCharacter("\n", 0), lines);
   string prefix = key + "=";
   int plen = StringLen(prefix);
   for(int i=0; i<n; i++)
   {
      string line = lines[i];
      if(StringLen(line) > 0 && StringGetCharacter(line, 0) == '\r')
         line = StringSubstr(line, 1);
      int last = StringLen(line) - 1;
      if(last >= 0 && StringGetCharacter(line, last) == '\r')
         line = StringSubstr(line, 0, last);
      if(StringFind(line, prefix) == 0)
         return StringSubstr(line, plen);
   }
   return "";
}

string Ok(string id)
{
   return "id=" + id + "\nok=1\n";
}

// Strip the characters the wire uses as structure. The Python side does the
// same on the way out (`_wire`, mt4_live.py:93-95); doing it on only one side
// is what let a broker comment containing a pipe shift every field after it.
// Both ends sanitise and neither trusts the other.
// Framing characters only: `_wire` also forces ASCII, which MQL4 has no cheap
// equivalent for. That asymmetry is stated in docs/MT4.md.
string Wire(string s)
{
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   StringReplace(s, "|", "/");
   return s;
}

string Fail(string id, int err, string msg)
{
   return "id=" + id + "\nok=0\nretcode=" + IntegerToString(err) + "\nerror=" + msg + "\n";
}

// Every failure from a trade handler states what it left behind.
// survivor=0 means the book was checked and nothing survived. A positive
// value is a live ticket the desk must deal with. The field is always
// present, so an adapter can tell an Expert that answered 0 from an
// Expert too old to answer at all.
string FailTrade(string id, int err, string msg, int survivor)
{
   return Fail(id, err, msg) + "survivor_ticket=" + IntegerToString(survivor) + "\n";
}

// A send whose OUTCOME IS UNKNOWN, which is not the same reply as a failure.
//
// `survivor_ticket` is OMITTED on purpose, and this is the one trade reply that
// omits it. docs/MT4.md already defines an absent field as COULD NOT MEASURE and
// the adapter maps it to None, which is exactly this state. Answering 0 would
// assert the book was checked and nothing survived -- the single thing we do not
// know here -- and the desk would close its in-flight entry on that assertion.
// So no new ICD field is needed; the existing absent-is-unmeasured rule carries
// it. The error token is what the adapter keys its unmeasured mapping on
// (constants.MT4_SEND_TIMEOUT_UNKNOWN).
string FailUnresolved(string id, int err, string msg)
{
   return Fail(id, err, msg);
}

int Tf(string name)
{
   if(name == "M1" || name == "1") return PERIOD_M1;
   if(name == "M5" || name == "5") return PERIOD_M5;
   if(name == "M15" || name == "15") return PERIOD_M15;
   if(name == "M30" || name == "30") return PERIOD_M30;
   if(name == "H1" || name == "60") return PERIOD_H1;
   if(name == "H4" || name == "240") return PERIOD_H4;
   if(name == "D1" || name == "1440") return PERIOD_D1;
   if(name == "W1") return PERIOD_W1;
   if(name == "MN1") return PERIOD_MN1;
   return PERIOD_H1;
}

int PendingType(string side, string kind)
{
   if(kind == "limit")
      return (side == "buy") ? OP_BUYLIMIT : OP_SELLLIMIT;
   return (side == "buy") ? OP_BUYSTOP : OP_SELLSTOP;
}

string KindOf(int typ)
{
   if(typ == OP_BUYLIMIT || typ == OP_SELLLIMIT) return "limit";
   if(typ == OP_BUYSTOP || typ == OP_SELLSTOP) return "stop";
   return "";
}

string SideOf(int typ)
{
   if(typ == OP_BUY || typ == OP_BUYLIMIT || typ == OP_BUYSTOP) return "buy";
   return "sell";
}

string ClipComment(string c)
{
   if(StringLen(c) <= 31)
      return c;
   return StringSubstr(c, 0, 31);
}

bool VolumeOk(string sym, double vol)
{
   double minlot = MarketInfo(sym, MODE_MINLOT);
   double maxlot = MarketInfo(sym, MODE_MAXLOT);
   double step = MarketInfo(sym, MODE_LOTSTEP);
   if(vol + 1e-8 < minlot) return false;
   if(vol - 1e-8 > maxlot) return false;
   if(step <= 0) return true;
   double steps = vol / step;
   return MathAbs(steps - MathRound(steps)) < 1e-6;
}

bool StopsOk(string sym, int typ, double price, double sl, double tp)
{
   double point = MarketInfo(sym, MODE_POINT);
   double level = MarketInfo(sym, MODE_STOPLEVEL) * point;
   RefreshRates();
   double bid = MarketInfo(sym, MODE_BID);
   double ask = MarketInfo(sym, MODE_ASK);
   if(typ == OP_BUY)
   {
      if(sl > 0 && bid - sl < level) return false;
      if(tp > 0 && tp - ask < level) return false;
      return true;
   }
   if(typ == OP_SELL)
   {
      if(sl > 0 && sl - ask < level) return false;
      if(tp > 0 && bid - tp < level) return false;
      return true;
   }
   if(typ == OP_BUYLIMIT)
   {
      if(ask - price < level) return false;
      if(sl > 0 && price - sl < level) return false;
      if(tp > 0 && tp - price < level) return false;
      return true;
   }
   if(typ == OP_SELLLIMIT)
   {
      if(price - bid < level) return false;
      if(sl > 0 && sl - price < level) return false;
      if(tp > 0 && price - tp < level) return false;
      return true;
   }
   if(typ == OP_BUYSTOP)
   {
      if(price - ask < level) return false;
      if(sl > 0 && price - sl < level) return false;
      if(tp > 0 && tp - price < level) return false;
      return true;
   }
   if(typ == OP_SELLSTOP)
   {
      if(bid - price < level) return false;
      if(sl > 0 && sl - price < level) return false;
      if(tp > 0 && price - tp < level) return false;
      return true;
   }
   return true;
}

// Find an order THIS send created, by the desk's client order id.
//
// A HIT is definitive. The desk stamps its client order id into the comment
// (`stamped_comment`, src/straightedge/inflight.py) and MT4 returns the comment
// on the book, so an order carrying it is this send and nothing else.
//
// A MISS PROVES NOTHING, and no caller may read it as "not sent". Brokers append
// to and overwrite OrderComment -- this repo ships a test for one rewritten to
// `rb-1/from #123` -- and a fill may simply not be in the local pool yet. That is
// why the caller answers UNKNOWN on a miss rather than failing: a lookup that
// re-sent on "not found" would have its failure in the dangerous direction,
// which is the reasoning already recorded in inflight.py.
//
// MODE_TRADES is the open AND pending pool, so one helper serves both send
// handlers. This reads the terminal's own pool, so it costs no broker round trip
// and no Sleep, and the Expert's declared ladder is unchanged.
int FindByClientId(string sym, int typ, int magic, string clientId)
{
   if(clientId == "")
      return -1;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderMagicNumber() != magic)
         continue;
      if(OrderSymbol() != sym)
         continue;
      if(OrderType() != typ)
         continue;
      if(StringFind(OrderComment(), clientId) < 0)
         continue;
      return OrderTicket();
   }
   return -1;
}

// `err` carries the error OUT. GetLastError() clears the register on read, so
// a caller that reads it a second time gets 0 and reports a rejection with no
// reason. The loop is the only place that can still see it.
//
// Returns a ticket, -1 for a clean rejection, or SE_SEND_UNKNOWN when the
// outcome could not be established. The three are different facts and the
// callers report them differently.
int SendRetry(string sym, int typ, double vol, double price, int slip, string comment, int magic, string clientId, int &err)
{
   int ticket = -1;
   err = 0;
   for(int i=0; i<SE_SEND_TRIES; i++)
   {
      RefreshRates();
      if(typ == OP_BUY) price = MarketInfo(sym, MODE_ASK);
      if(typ == OP_SELL) price = MarketInfo(sym, MODE_BID);
      ticket = OrderSend(sym, typ, vol, price, slip, 0, 0, comment, magic, 0, clrNONE);
      if(ticket >= 0)
         return ticket;
      err = GetLastError();
      // 128 is ERR_TRADE_TIMEOUT and it is NOT a failure: the server has the
      // request and the REPLY was lost, so the outcome is unknown. Re-sending is
      // how one order becomes two, so the ladder STOPS here and the book is
      // asked instead. MQL4's own guidance is to confirm the order did not go
      // through before re-sending.
      //
      // 146 (ERR_TRADE_CONTEXT_BUSY) and 141 (ERR_TOO_MANY_REQUESTS) are
      // refusals taken BEFORE the request goes out, so repeating those is the
      // same order's next attempt and they keep their retry.
      if(err == 128)
      {
         int settled = FindByClientId(sym, typ, magic, clientId);
         if(settled > 0)
         {
            err = 0;
            return settled;
         }
         return SE_SEND_UNKNOWN;
      }
      if(err != 146 && err != 141)
         break;
      Sleep(SE_RETRY_SLEEP_MS);
   }
   return -1;
}

// Same contract as SendRetry. The OrderSelect branch used to return without
// reading the register at all, which left the caller reading a DIFFERENT call's
// error rather than nothing; that is a wrong answer, not a missing one.
bool ModifyRetry(int ticket, double price, double sl, double tp, int &err)
{
   err = 0;
   for(int i=0; i<SE_MODIFY_TRIES; i++)
   {
      RefreshRates();
      if(!OrderSelect(ticket, SELECT_BY_TICKET))
      {
         err = GetLastError();
         return false;
      }
      if(OrderModify(ticket, price, sl, tp, 0, clrNONE))
         return true;
      err = GetLastError();
      if(err != 146 && err != 1)
         return false;
      Sleep(SE_RETRY_SLEEP_MS);
   }
   return false;
}

// 1 = still on the book, 0 = confirmed gone, -1 = cannot tell.
// Leaves the ticket selected when it returns 1.
int TicketState(int ticket)
{
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return -1;
   if(OrderCloseTime() != 0)
      return 0;
   return 1;
}

// Close a position this Expert has just opened but could not protect.
// Returns true ONLY when the book confirms the position is gone. OrderClose's
// own return value is recorded and logged, never treated as proof: it is a
// claim about the book, and the book is the artifact.
bool RollbackPosition(int ticket, string sym, int slip)
{
   for(int i=0; i<SE_ROLLBACK_TRIES; i++)
   {
      int state = TicketState(ticket);
      if(state == 0)
         return true;
      if(state == 1)
      {
         RefreshRates();
         double px = (OrderType() == OP_BUY) ? MarketInfo(sym, MODE_BID) : MarketInfo(sym, MODE_ASK);
         bool sent = OrderClose(ticket, OrderLots(), px, slip, clrNONE);
         if(!sent)
            Print("mt4riskbot rollback close failed ticket=", ticket, " err=", GetLastError());
      }
      Sleep(SE_RETRY_SLEEP_MS);
   }
   return TicketState(ticket) == 0;
}

// Same contract for a pending order that was placed but could not be protected.
bool RollbackPending(int ticket)
{
   for(int i=0; i<SE_ROLLBACK_TRIES; i++)
   {
      int state = TicketState(ticket);
      if(state == 0)
         return true;
      if(state == 1)
      {
         bool sent = OrderDelete(ticket);
         if(!sent)
            Print("mt4riskbot rollback delete failed ticket=", ticket, " err=", GetLastError());
      }
      Sleep(SE_RETRY_SLEEP_MS);
   }
   return TicketState(ticket) == 0;
}

string Handle(string body)
{
   string id = KV(body, "id");
   string op = KV(body, "op");
   if(op == "ping")
      // The desk reads these three and refuses to stay quiet about a send budget
      // that does not clear `ladder_ms`. Declared on every ping rather than
      // documented once, because the ladder bounds are `input` parameters and an
      // operator can change them in the terminal's dialog on a box nobody is
      // watching; a number in a runbook cannot go red, this can.
      return Ok(id)
         + "time=" + IntegerToString((int)TimeCurrent()) + "\n"
         + "ladder_ms=" + IntegerToString(SE_LADDER_SLEEP_MS) + "\n"
         + "broker_calls=" + IntegerToString(SE_BROKER_CALLS) + "\n"
         + "fence=" + (gFileTimeKnown ? "1" : "0") + "\n";
   if(op == "account")
      return AccountReply(id);
   if(op == "tick")
      return TickReply(id, KV(body, "symbol"));
   if(op == "symbol")
      return SymbolReply(id, KV(body, "symbol"));
   if(op == "select")
      return SelectReply(id, KV(body, "symbol"));
   if(op == "rates")
      return RatesReply(id, KV(body, "symbol"), KV(body, "timeframe"), KV(body, "count"));
   if(op == "positions")
      return BookReply(id, KV(body, "magic"), false);
   if(op == "orders")
      return BookReply(id, KV(body, "magic"), true);
   if(op == "check_market")
      return CheckMarket(id, body, false);
   if(op == "market")
      return CheckMarket(id, body, true);
   if(op == "check_working")
      return CheckWorking(id, body, false);
   if(op == "working")
      return CheckWorking(id, body, true);
   if(op == "modify_position")
      return ModifyPos(id, body);
   if(op == "modify_working")
      return ModifyPend(id, body);
   if(op == "cancel")
      return CancelOrder(id, body);
   if(op == "close")
      return ClosePos(id, body);
   if(op == "close_by")
      return CloseBy(id, body);
   return Fail(id, 1, "unsupported");
}

string AccountReply(string id)
{
   int mode = IsDemo() ? 0 : 2;
   int allowed = (IsTradeAllowed() && IsExpertEnabled() && IsConnected()) ? 1 : 0;
   int expert = IsExpertEnabled() ? 1 : 0;
   return Ok(id)
      + "login=" + IntegerToString(AccountNumber()) + "\n"
      + "balance=" + DoubleToString(AccountBalance(), 2) + "\n"
      + "equity=" + DoubleToString(AccountEquity(), 2) + "\n"
      + "margin=" + DoubleToString(AccountMargin(), 2) + "\n"
      + "margin_free=" + DoubleToString(AccountFreeMargin(), 2) + "\n"
      + "profit=" + DoubleToString(AccountProfit(), 2) + "\n"
      + "currency=" + Wire(AccountCurrency()) + "\n"
      + "leverage=" + IntegerToString(AccountLeverage()) + "\n"
      + "trade_mode=" + IntegerToString(mode) + "\n"
      + "trade_allowed=" + IntegerToString(allowed) + "\n"
      + "trade_expert=" + IntegerToString(expert) + "\n"
      + "name=" + Wire(AccountName()) + "\n"
      + "server=" + Wire(AccountServer()) + "\n";
}

string TickReply(string id, string sym)
{
   if(sym == "")
      return Fail(id, 1, "symbol");
   RefreshRates();
   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   return Ok(id)
      + "bid=" + DoubleToString(MarketInfo(sym, MODE_BID), digits) + "\n"
      + "ask=" + DoubleToString(MarketInfo(sym, MODE_ASK), digits) + "\n"
      + "time=" + IntegerToString((int)TimeCurrent()) + "\n";
}

string SymbolReply(string id, string sym)
{
   if(sym == "")
      return Fail(id, 1, "symbol");
   SymbolSelect(sym, true);
   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   return Ok(id)
      + "digits=" + IntegerToString(digits) + "\n"
      + "point=" + DoubleToString(MarketInfo(sym, MODE_POINT), digits) + "\n"
      + "volume_min=" + DoubleToString(MarketInfo(sym, MODE_MINLOT), 8) + "\n"
      + "volume_max=" + DoubleToString(MarketInfo(sym, MODE_MAXLOT), 8) + "\n"
      + "volume_step=" + DoubleToString(MarketInfo(sym, MODE_LOTSTEP), 8) + "\n"
      + "tick_value=" + DoubleToString(MarketInfo(sym, MODE_TICKVALUE), 8) + "\n"
      + "tick_size=" + DoubleToString(MarketInfo(sym, MODE_TICKSIZE), digits) + "\n"
      + "contract_size=" + DoubleToString(MarketInfo(sym, MODE_LOTSIZE), 0) + "\n"
      + "stops_level=" + IntegerToString((int)MarketInfo(sym, MODE_STOPLEVEL)) + "\n"
      + "freeze_level=" + IntegerToString((int)MarketInfo(sym, MODE_FREEZELEVEL)) + "\n"
      + "spread=" + IntegerToString((int)MarketInfo(sym, MODE_SPREAD)) + "\n";
}

string SelectReply(string id, string sym)
{
   if(sym == "")
      return Fail(id, 1, "symbol");
   if(!SymbolSelect(sym, true))
      return Fail(id, 1, "select");
   return Ok(id);
}

// A price series exists per symbol AND timeframe, and MT4 builds one only once
// something asks for it. A terminal with H4 charts open and a desk configured
// for H1 therefore has H1 history for nothing, which was measured on the live
// rig as EURUSD bars=0 / ATR=nan while XAUUSD, the one charted symbol, read
// bars=200. Three things this handler did not do, each of which left a cold
// symbol permanently untradeable with nothing said anywhere:
//
//  1. It never selected the symbol. Every other symbol-scoped handler does
//     (SymbolReply, SelectReply). `rates` worked only because Engine.start()
//     happens to call `select` for each configured symbol first, which is call
//     order, not a guarantee this handler made for itself.
//  2. At iBars()==0 it set n=0, so the row loop never executed and NO
//     price-series function was reached at all: no iTime, iOpen, iClose,
//     iVolume, no ArrayCopyRates. iBars alone is not the documented download
//     trigger; the iXXX series access is, and it reports 4066
//     ERR_HISTORY_WILL_UPDATED while the request is in flight. The handler
//     early-returned on exactly the series it needed to ask for, so the desk
//     could not warm a symbol no matter how many times it asked.
//  3. It discarded GetLastError() and emitted only `n=`. That made `ok=1 n=0`
//     one wire value for three different worlds: downloading right now, not
//     served by this broker under this name, and genuinely empty. A desk
//     cannot choose between "wait" and "name it and stop" from a value that
//     cannot tell those apart, and n=0 is the reassuring reading of the three.
//
// No Sleep here. This handler IS the mailbox (Process(), single threaded behind
// gBusy) and the adapter's bridge times out at 5 s, so a retry loop in here
// would stall every other op and blow that timeout. This end triggers the
// fetch once and reports what happened; the desk owns the bounded wait
// (straightedge/history.py:preflight).
string RatesReply(string id, string sym, string tfName, string countStr)
{
   if(sym == "")
      return Fail(id, 1, "symbol");
   int tf = Tf(tfName);
   int want = (int)StringToInteger(countStr);
   if(want <= 0) want = 1;
   bool selected = SymbolSelect(sym, true);
   ResetLastError();
   int total = iBars(sym, tf);
   int history_error = 0;
   if(total < want)
   {
      ResetLastError();
      double first = iClose(sym, tf, 0);
      history_error = GetLastError();
      ResetLastError();
      total = iBars(sym, tf);
      if(total <= 0 && first != 0.0)
         Print("mt4riskbot rates ", sym, " ", tfName,
               " served a close with zero bars; history state is inconsistent");
   }
   int n = want;
   if(n > total) n = total;
   string rows = "";
   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   int written = 0;
   for(int i=n-1; i>=0; i--)
   {
      string line = IntegerToString((int)iTime(sym, tf, i))
         + "|" + DoubleToString(iOpen(sym, tf, i), digits)
         + "|" + DoubleToString(iHigh(sym, tf, i), digits)
         + "|" + DoubleToString(iLow(sym, tf, i), digits)
         + "|" + DoubleToString(iClose(sym, tf, i), digits)
         + "|" + IntegerToString((int)iVolume(sym, tf, i));
      rows = rows + "row" + IntegerToString(written) + "=" + line + "\n";
      written++;
   }
   // bars_total, selected and history_error are emitted on EVERY rates reply,
   // including the healthy one. A field present only on failure cannot be told
   // from an Expert too old to emit it at all, which is the partition
   // survivor_ticket exists for above.
   return Ok(id)
      + "n=" + IntegerToString(written) + "\n"
      + "bars_total=" + IntegerToString(total) + "\n"
      + "selected=" + IntegerToString(selected ? 1 : 0) + "\n"
      + "history_error=" + IntegerToString(history_error) + "\n"
      + rows;
}

string BookReply(string id, string magicStr, bool pending)
{
   int want = (int)StringToInteger(magicStr);
   string rows = "";
   int written = 0;
   int total = OrdersTotal();
   for(int i=0; i<total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      int typ = OrderType();
      bool isPend = (typ > OP_SELL);
      if(isPend != pending)
         continue;
      if(want != 0 && OrderMagicNumber() != want)
         continue;
      int digits = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
      string line;
      if(pending)
      {
         line = IntegerToString(OrderTicket())
            + "|" + Wire(OrderSymbol())
            + "|" + SideOf(typ)
            + "|" + KindOf(typ)
            + "|" + DoubleToString(OrderLots(), 2)
            + "|" + DoubleToString(OrderOpenPrice(), digits)
            + "|" + DoubleToString(OrderStopLoss(), digits)
            + "|" + DoubleToString(OrderTakeProfit(), digits)
            + "|" + IntegerToString(OrderMagicNumber())
            + "|" + Wire(OrderComment())
            + "|" + IntegerToString((int)OrderOpenTime());
      }
      else
      {
         line = IntegerToString(OrderTicket())
            + "|" + Wire(OrderSymbol())
            + "|" + SideOf(typ)
            + "|" + DoubleToString(OrderLots(), 2)
            + "|" + DoubleToString(OrderOpenPrice(), digits)
            + "|" + DoubleToString(OrderStopLoss(), digits)
            + "|" + DoubleToString(OrderTakeProfit(), digits)
            + "|" + DoubleToString(OrderClosePrice(), digits)
            + "|" + DoubleToString(OrderProfit(), 2)
            + "|" + IntegerToString(OrderMagicNumber())
            + "|" + Wire(OrderComment())
            + "|" + DoubleToString(OrderSwap(), 2)
            + "|" + IntegerToString((int)OrderOpenTime());
      }
      rows = rows + "row" + IntegerToString(written) + "=" + line + "\n";
      written++;
   }
   return Ok(id) + "n=" + IntegerToString(written) + "\n" + rows;
}

string CheckMarket(string id, string body, bool send)
{
   string sym = KV(body, "symbol");
   string side = KV(body, "side");
   double vol = StringToDouble(KV(body, "volume"));
   double sl = StringToDouble(KV(body, "sl"));
   double tp = StringToDouble(KV(body, "tp"));
   int magic = (int)StringToInteger(KV(body, "magic"));
   int slip = (int)StringToInteger(KV(body, "deviation"));
   if(slip <= 0) slip = Slippage;
   if(sym == "" || (side != "buy" && side != "sell"))
      return FailTrade(id, 1, "symbol", 0);
   if(!IsTradeAllowed() || !IsExpertEnabled())
      return FailTrade(id, 133, "trade_disabled", 0);
   SymbolSelect(sym, true);
   if(!VolumeOk(sym, vol))
      return FailTrade(id, 131, "invalid_volume", 0);
   int typ = (side == "buy") ? OP_BUY : OP_SELL;
   RefreshRates();
   double price = (typ == OP_BUY) ? MarketInfo(sym, MODE_ASK) : MarketInfo(sym, MODE_BID);
   if(!StopsOk(sym, typ, price, sl, tp))
      return FailTrade(id, 130, "invalid_stops", 0);
   if(!send)
      return Ok(id) + "ticket=0\nprice=" + DoubleToString(price, (int)MarketInfo(sym, MODE_DIGITS)) + "\n";
   int sendErr = 0;
   int ticket = SendRetry(sym, typ, vol, price, slip, ClipComment(KV(body, "comment")), magic, KV(body, "client_id"), sendErr);
   // The desk's client order id, logged on every outcome. This is what lets a
   // post-incident reconcile join this log to the desk's journal after a send the
   // desk never got an answer for, which is the only case where the two records
   // disagree and the only case where it matters.
   Print("mt4riskbot send op=market client_id=", KV(body, "client_id"),
         " symbol=", sym, " lots=", DoubleToString(vol, 2),
         " ticket=", ticket, " err=", sendErr);
   if(ticket == SE_SEND_UNKNOWN)
      return FailUnresolved(id, 128, "send_timeout_outcome_unknown");
   if(ticket < 0)
      return FailTrade(id, sendErr, "OrderSend", 0);
   if(sl > 0 || tp > 0)
   {
      // A position exists from here on. Failing to select it is not a reason
      // to abandon it; it is a reason to roll it back.
      double openPrice = 0;
      int modErr = 0;
      if(OrderSelect(ticket, SELECT_BY_TICKET))
         openPrice = OrderOpenPrice();
      else
         modErr = GetLastError();
      if(openPrice <= 0 || !ModifyRetry(ticket, openPrice, sl, tp, modErr))
      {
         if(!RollbackPosition(ticket, sym, slip))
            return FailTrade(id, modErr, "sl_modify_failed_position_live", ticket);
         return FailTrade(id, modErr, "sl_modify_failed", 0);
      }
   }
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return Ok(id) + "ticket=" + IntegerToString(ticket) + "\n";
   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   return Ok(id)
      + "ticket=" + IntegerToString(ticket) + "\n"
      + "volume=" + DoubleToString(OrderLots(), 2) + "\n"
      + "price=" + DoubleToString(OrderOpenPrice(), digits) + "\n";
}

string CheckWorking(string id, string body, bool send)
{
   string sym = KV(body, "symbol");
   string side = KV(body, "side");
   string kind = KV(body, "kind");
   double vol = StringToDouble(KV(body, "volume"));
   double price = StringToDouble(KV(body, "price"));
   double sl = StringToDouble(KV(body, "sl"));
   double tp = StringToDouble(KV(body, "tp"));
   int magic = (int)StringToInteger(KV(body, "magic"));
   // Same three lines as CheckMarket and ClosePos, and until #92 this handler
   // had none of them: it passed the Expert's own Slippage input straight to
   // SendRetry, so the desk deviation gate judged a tolerance this send never
   // offered. The <= 0 fallback is the pre-#92 behaviour for an older desk that
   // sends no deviation key at all, which is the only case it now covers.
   int slip = (int)StringToInteger(KV(body, "deviation"));
   if(slip <= 0) slip = Slippage;
   if(sym == "" || (side != "buy" && side != "sell") || (kind != "limit" && kind != "stop"))
      return FailTrade(id, 1, "symbol", 0);
   if(!IsTradeAllowed() || !IsExpertEnabled())
      return FailTrade(id, 133, "trade_disabled", 0);
   SymbolSelect(sym, true);
   if(!VolumeOk(sym, vol))
      return FailTrade(id, 131, "invalid_volume", 0);
   int typ = PendingType(side, kind);
   if(!StopsOk(sym, typ, price, sl, tp))
      return FailTrade(id, 130, "invalid_stops", 0);
   if(!send)
      return Ok(id) + "ticket=0\n";
   int sendErr = 0;
   int ticket = SendRetry(sym, typ, vol, price, slip, ClipComment(KV(body, "comment")), magic, KV(body, "client_id"), sendErr);
   if(ticket == SE_SEND_UNKNOWN)
      return FailUnresolved(id, 128, "send_timeout_outcome_unknown");
   if(ticket < 0)
      return FailTrade(id, sendErr, "OrderSend", 0);
   if(sl > 0 || tp > 0)
   {
      int modErr = 0;
      if(!ModifyRetry(ticket, price, sl, tp, modErr))
      {
         if(!RollbackPending(ticket))
            return FailTrade(id, modErr, "sl_modify_failed_order_live", ticket);
         return FailTrade(id, modErr, "sl_modify_failed", 0);
      }
   }
   return Ok(id) + "ticket=" + IntegerToString(ticket) + "\nprice=" + DoubleToString(price, (int)MarketInfo(sym, MODE_DIGITS)) + "\n";
}

string ModifyPos(string id, string body)
{
   int ticket = (int)StringToInteger(KV(body, "ticket"));
   double sl = StringToDouble(KV(body, "sl"));
   double tp = StringToDouble(KV(body, "tp"));
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return Fail(id, 4108, "not_found");
   if(OrderType() > OP_SELL)
      return Fail(id, 1, "not_position");
   int modErr = 0;
   if(!ModifyRetry(ticket, OrderOpenPrice(), sl, tp, modErr))
      return Fail(id, modErr, "OrderModify");
   return Ok(id) + "ticket=" + IntegerToString(ticket) + "\n";
}

string ModifyPend(string id, string body)
{
   int ticket = (int)StringToInteger(KV(body, "ticket"));
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return Fail(id, 4108, "not_found");
   if(OrderType() <= OP_SELL)
      return Fail(id, 1, "not_pending");
   double price = OrderOpenPrice();
   double sl = OrderStopLoss();
   double tp = OrderTakeProfit();
   string p = KV(body, "price");
   string s = KV(body, "sl");
   string t = KV(body, "tp");
   if(p != "") price = StringToDouble(p);
   if(s != "") sl = StringToDouble(s);
   if(t != "") tp = StringToDouble(t);
   int modErr = 0;
   if(!ModifyRetry(ticket, price, sl, tp, modErr))
      return Fail(id, modErr, "OrderModify");
   return Ok(id) + "ticket=" + IntegerToString(ticket) + "\n";
}

string CancelOrder(string id, string body)
{
   int ticket = (int)StringToInteger(KV(body, "ticket"));
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return Fail(id, 4108, "not_found");
   if(OrderType() <= OP_SELL)
      return Fail(id, 1, "not_pending");
   if(!OrderDelete(ticket))
      return Fail(id, GetLastError(), "OrderDelete");
   return Ok(id) + "ticket=" + IntegerToString(ticket) + "\n";
}

string ClosePos(string id, string body)
{
   int ticket = (int)StringToInteger(KV(body, "ticket"));
   double vol = StringToDouble(KV(body, "volume"));
   double price = StringToDouble(KV(body, "price"));
   int slip = (int)StringToInteger(KV(body, "deviation"));
   if(slip <= 0) slip = Slippage;
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return Fail(id, 4108, "not_found");
   if(OrderType() > OP_SELL)
      return Fail(id, 1, "not_position");
   string sym = OrderSymbol();
   RefreshRates();
   if(price <= 0)
      price = (OrderType() == OP_BUY) ? MarketInfo(sym, MODE_BID) : MarketInfo(sym, MODE_ASK);
   if(vol <= 0 || vol > OrderLots() + 1e-8)
      vol = OrderLots();
   if(!OrderClose(ticket, vol, price, slip, clrNONE))
      return Fail(id, GetLastError(), "OrderClose");
   return Ok(id) + "ticket=" + IntegerToString(ticket) + "\nvolume=" + DoubleToString(vol, 2) + "\n";
}

string CloseBy(string id, string body)
{
   int ticket = (int)StringToInteger(KV(body, "ticket"));
   int other = (int)StringToInteger(KV(body, "other"));
   if(!OrderCloseBy(ticket, other, clrNONE))
      return Fail(id, GetLastError(), "OrderCloseBy");
   return Ok(id) + "ticket=" + IntegerToString(ticket) + "\n";
}
