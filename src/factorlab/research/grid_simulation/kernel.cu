#ifdef GRID_CPU
#include <math.h>
#include <algorithm>
#include <cstdint>
#define __device__
using std::min;
static unsigned long long __umul64hi(unsigned long long a, unsigned long long b) {
    unsigned long long a0=static_cast<unsigned int>(a),a1=a>>32;
    unsigned long long b0=static_cast<unsigned int>(b),b1=b>>32;
    unsigned long long low=a0*b0,carry=(low>>32)+a1*b0;
    unsigned long long middle=static_cast<unsigned int>(carry)+a0*b1;
    return a1*b1+(carry>>32)+(middle>>32);
}
#endif
#define MAX_ORDERS 128
#define MAX_LEVELS 33
#define MAX_AUDIT 512
#define SCALE 1000000LL
#define COLS 26
struct Order {
    long long limit;
    int quantity, side, level, created, status, cancel, id;
    double reservation;
};
struct Account {
    Order orders[MAX_ORDERS];
    int n, inventory, state, reason, fills, error, issued;
    int remainder[2*MAX_LEVELS];
    double cash, entry, price, peak, nav_peak, turnover, fees, slip, max_dd, funding;
    double center, spacing, pending_center, pending_spacing, volatility, half_width, funding_bps;
    int rebuilds, last_rebuild, pending_rebuild, blocked, history_count, history_head;
    long long entry_time;
    int signal_low, signal_end;
    double signal_sum, paid_bps;
    double *audit;
    int audit_count;
};
__device__ void emit(Account &a, int kind, int id=0, int side=0, int level=-1,
    double quantity=0, double price=0, double value=0, double reservation=0, double extra=0) {
    if(!a.audit) return;
    if(a.audit_count==MAX_AUDIT) {a.error=6; return;}
    double *out=a.audit+(a.audit_count++)*11;
    out[0]=kind; out[1]=id; out[2]=side; out[3]=level; out[4]=quantity;
    out[5]=price; out[6]=value; out[7]=reservation; out[8]=a.state; out[9]=a.reason; out[10]=extra;
}
__device__ bool legal_notional(unsigned long long price, int quantity, const long long *v) {
    unsigned long long q=quantity*static_cast<unsigned long long>(v[9]);
    unsigned long long minimum=v[10], scale=1000000000000ULL;
    unsigned long long left_hi=__umul64hi(price,q), left_lo=price*q;
    unsigned long long right_hi=__umul64hi(minimum,scale), right_lo=minimum*scale;
    return left_hi>right_hi || (left_hi==right_hi && left_lo>=right_lo);
}
__device__ bool ambiguous(Account &a, double left, double right, const double *p) {
    if(fabs(left-right)<=p[9]) {a.error=3; return true;}
    return false;
}
__device__ double equity(Account &a, const double *p) {
    return a.cash+a.inventory*p[1]*(a.price-a.entry);
}
__device__ double reserved(Account &a, const double *p) {
    double value=a.inventory*p[1]*a.entry;
    for(int j=0;j<a.n;j++) if(a.orders[j].status==2 && a.orders[j].side==0)
        value+=a.orders[j].quantity*p[1]*a.orders[j].reservation*(1+p[3]);
    return value;
}
__device__ void cancel_order(Account &a, Order &o, const double *p) {
    if(o.status && !o.cancel) {
        o.cancel=1;
        emit(a,2,o.id,o.side,o.level,o.quantity*p[1],double(o.limit)/SCALE);
    }
}
__device__ void cancel_all(Account &a, const double *p) {
    for(int j=0;j<a.n;j++) cancel_order(a,a.orders[j],p);
}
__device__ void clear_remainders(Account &a) {
    for(int j=0;j<2*MAX_LEVELS;j++) a.remainder[j]=0;
}
__device__ void stop(Account &a, int reason, bool exit, const double *p) {
    clear_remainders(a); a.pending_rebuild=0;
    cancel_all(a,p);
    a.state=exit && a.inventory?2:1; a.reason=reason;
    emit(a,7);
}
__device__ void risk(Account &a, const double *p) {
    double nav=equity(a,p);
    a.peak=fmax(a.peak,nav);
    if(a.state!=0 && a.state!=4) return;
    if(ambiguous(a,nav,a.peak*(1-p[6]),p) || ambiguous(a,a.turnover,p[0]*p[7],p)) return;
    if(1-nav/a.peak>=p[6]) stop(a,2,false,p);
    else if(a.turnover/p[0]>=p[7]) stop(a,3,false,p);
}
__device__ void nav_observation(Account &a, const double *p) {
    double nav=equity(a,p);
    a.nav_peak=fmax(a.nav_peak,nav);
    a.max_dd=fmax(a.max_dd,1-nav/a.nav_peak);
}
__device__ void propose(Account &a, int side, int level, int quantity, int step,
    const double *targets, const long long *limits, const double *p, const long long *v, bool market=false) {
    if(!(a.state==0 || (a.state==2 && market && side==1)) || (side==0 && a.blocked)) return;
    if(quantity<v[6] || quantity<=0) return;
    double target=market?a.price:targets[level];
    long long limit=market?0:limits[side*v[0]+level];
    double price=market?a.price:double(limit)/SCALE;
    long long numerator=market?static_cast<long long>(llround(a.price*SCALE))*SCALE:limit*SCALE;
    if((!market && price<p[8]) || !legal_notional(numerator,quantity,v)) return;
    int buys=0,sells=0;
    double pending=0;
    for(int j=0;j<a.n;j++) {
        Order &o=a.orders[j];
        if(!o.status) continue;
        if(o.side==0) {
            buys+=o.quantity;
            if(o.status==1) pending+=o.quantity*p[1]*fmax(a.price,double(o.limit)/SCALE)*(1+p[3]+p[4]);
        } else sells+=o.quantity;
    }
    double cost=quantity*p[1]*price*(1+p[3]+p[4]), committed=reserved(a,p);
    if(side==1) {if(quantity>a.inventory-sells) return;}
    else {
        if(a.inventory+buys+quantity>v[5]) return;
        if(ambiguous(a,pending+cost,a.cash-committed,p) || ambiguous(a,committed+pending+cost,p[5],p)) return;
        if(pending+cost>a.cash-committed || committed+pending+cost>p[5]) return;
    }
    int adjacent=level+(side==0?1:-1);
    if(!market && adjacent>=0 && adjacent<v[0]) {
        double other=targets[adjacent];
        if(ambiguous(a,fabs(other-target),(price+other)*(p[3]+p[4]),p)) return;
        if(fabs(other-target)<=(price+other)*(p[3]+p[4])) return;
    }
    if(a.n==MAX_ORDERS) {a.error=1; return;}
    Order o;
    o.limit=limit; o.quantity=quantity; o.side=side; o.level=market?-1:level;
    o.created=step; o.status=1; o.cancel=0; o.reservation=0; o.id=++a.issued;
    a.orders[a.n++]=o;
    emit(a,1,o.id,side,o.level,quantity*p[1],target,double(limit)/SCALE,0,market?1:0);
}
__device__ void seed(Account &a,int step,const double *targets,const long long *limits,const double *p,const long long *v) {
    for(int j=0;j<v[0];j++) if(targets[j]!=a.price)
        propose(a,targets[j]<a.price?0:1,j,v[3],step,targets,limits,p,v);
}
__device__ bool dynamic_limits(Account &a,const double *targets,long long *limits,const double *p,const long long *v) {
    long long tick=llround(p[12]*SCALE);
    for(int j=0;j<v[0];j++) {
        if(targets[j]>1000000 || targets[j]<=0) {a.error=4; return false;}
        double units=targets[j]/p[12];
        long long nearest=static_cast<long long>(round(units));
        long long floor_tick=static_cast<long long>(floor(units)),ceil_tick=static_cast<long long>(ceil(units));
        // A six-decimal tick has a unique correctly-rounded binary representative.
        // Comparing with it preserves Decimal(str(target)) direction even when
        // target/tick rounds to an integer on either neighboring binary value.
        if(fabs(units-nearest)<32*2.220446049250313e-16*fmax(1.,fabs(units))) {
            double boundary=double(nearest*tick)/SCALE;
            floor_tick=targets[j]<boundary?nearest-1:nearest;
            ceil_tick=targets[j]>boundary?nearest+1:nearest;
        }
        limits[j]=floor_tick*tick;
        limits[v[0]+j]=ceil_tick*tick;
    }
    return true;
}
__device__ void settle(Account &a,int step,double *targets,long long *limits,const double *p,const long long *v) {
    for(int j=0;j<a.n;j++) if(a.orders[j].status) return;
    if(a.pending_rebuild && (a.state==0 || a.state==4)) {
        double center=a.pending_center, spacing=a.pending_spacing;
        a.pending_rebuild=0;
        double lower,upper;
        if(spacing>=0) {lower=center-(v[0]-1)*spacing/2; upper=center+(v[0]-1)*spacing/2;}
        else if(v[12]==1) {lower=center/sqrt(p[25]); upper=center*sqrt(p[25]);}
        else {lower=center-p[26]/2; upper=center+p[26]/2;}
        if(lower<p[8]) {a.state=1; a.reason=5; emit(a,7); return;}
        double ratio=pow(upper/lower,1./(v[0]-1));
        for(int j=0;j<v[0];j++) targets[j]=spacing>=0?lower+j*spacing:
            (v[12]==1?lower*pow(ratio,j):lower+(upper-lower)*j/(v[0]-1));
        if(!dynamic_limits(a,targets,limits,p,v)) return;
        a.center=center; a.spacing=(upper-lower)/(v[0]-1); a.last_rebuild=step; a.rebuilds++; a.state=0;
        emit(a,5,0,0,-1,0,center,a.spacing,0,a.rebuilds);
        seed(a,step,targets,limits,p,v);
    } else if(a.state==2) {
        if(a.inventory) propose(a,1,-1,a.inventory,step,targets,limits,p,v,true);
        else {a.state=3; emit(a,7);}
    }
}
__device__ void rebuild(Account &a,double center,double spacing,int step,double *targets,long long *limits,const double *p,const long long *v) {
    clear_remainders(a); cancel_all(a,p);
    a.pending_center=center; a.pending_spacing=spacing; a.pending_rebuild=1;
    settle(a,step,targets,limits,p,v);
}
__device__ unsigned long long rounded_product(Account &a,unsigned long long x,unsigned long long y,unsigned long long d) {
    unsigned long long lo=x*y, hi=__umul64hi(x,y);
    if(hi>=d) {a.error=4; return 0;}
    unsigned long long q=0,r=hi;
    for(int i=63;i>=0;i--) {
        bool overflow=(r>>63)!=0;
        r=(r<<1)|((lo>>i)&1);
        if(overflow || r>=d) {r-=d; q|=1ULL<<i;}
    }
    if(r>d-r || (r==d-r && (q&1))) {
        if(q==0xffffffffffffffffULL) {a.error=4; return 0;}
        q++;
    }
    if(q>9007199254740991ULL) a.error=4;
    return q;
}
__device__ double volatility(double *history,int n,const double *p,const long long *v) {
    int count=min(n,int(v[16])+1);
    if(count<3) return p[19];
    double mean=0;
    for(int j=n-count+1;j<n;j++) mean+=log(history[j])-log(history[j-1]);
    mean/=count-1;
    double sum=0;
    for(int j=n-count+1;j<n;j++) {double delta=(log(history[j])-log(history[j-1]))-mean; sum+=delta*delta;}
    double value=sqrt(sum/(count-2));
    return isfinite(value) && value>1e-8?value:p[19];
}
__device__ double half_width(double vol,const double *p) {
    return fmax(p[15],fmin(p[16],p[13]*vol*sqrt(p[14])));
}
__device__ double funding_signal(Account &a,long long now,const long long *times,const double *rates,int events,const long long *v) {
    long long start=now-v[19]*3600000000LL;
    while(a.signal_end<events && times[a.signal_end]<=now) a.signal_sum+=rates[a.signal_end++]*10000;
    while(a.signal_low<a.signal_end && times[a.signal_low]<=start) a.signal_sum-=rates[a.signal_low++]*10000;
    int end=a.signal_end;
    if(a.signal_low==end) {a.signal_sum=0; return 0;}
    double gap=end>=2?fmax(1.,fmin(8.,double(times[end-1]-times[end-2])/3600000000.)):8.;
    double average=a.signal_sum/v[19],last=rates[end-1]*10000/gap;
    return v[20]?fmax(average,last):average;
}
__device__ void grid_path(const long long *quotes,int path,int steps,const double *initial_targets,
    const long long *initial_limits,const double *p,const long long *v,int trace,double *output,double *fills,int *errors,
    const double *bars,const long long *times,const long long *funding_times,const double *rates_all,const double *marks_all,
    const long long *coeff_all,const double *warmup,double *workspace,int events,int warmup_count,int width,double *audit) {
    Account a={};
    a.inventory=v[4]; a.state=v[12]==2?4:0;
    a.cash=p[0]; a.entry=double(quotes[path*steps])/SCALE; a.price=a.entry;
    a.center=a.price; a.peak=p[0]; a.nav_peak=p[0]; a.entry_time=-1;
    double targets[MAX_LEVELS]; long long limits[2*MAX_LEVELS];
    for(int j=0;j<v[0];j++) {targets[j]=initial_targets[j]; limits[j]=initial_limits[j]; limits[v[0]+j]=initial_limits[v[0]+j];}
    a.spacing=(targets[v[0]-1]-targets[0])/(v[0]-1);
    double *history=workspace+path*width;
    if(v[11]==1) {
        a.history_count=min(warmup_count,int(v[16])+1);
        for(int j=0;j<a.history_count;j++) history[j]=warmup[path*warmup_count+warmup_count-a.history_count+j];
        a.volatility=volatility(history,a.history_count,p,v); a.half_width=half_width(a.volatility,p);
    }
    const double *rates=rates_all+path*events,*marks=marks_all+path*events;
    const long long *coeff=coeff_all+path*events*2;
    int funding_cursor=0;
    while(funding_cursor<events && funding_times[funding_cursor]<=times[0]) funding_cursor++;
    double previous_close=0;
    for(int t=0;t<steps;t++) {
        int step=t+1,prior=a.n;
        a.audit=trace?audit+((path*steps+t)*MAX_AUDIT)*11:nullptr; a.audit_count=0;
        double day_fee=0,day_slip=0,day_funding=0;
        while(funding_cursor<events && funding_times[funding_cursor]<=times[t]) {
            int e=funding_cursor++;
            long long num=coeff[2*e];
            unsigned long long magnitude=num<0?static_cast<unsigned long long>(-(num+1))+1:static_cast<unsigned long long>(num);
            unsigned long long units=rounded_product(a,a.inventory,magnitude,coeff[2*e+1]);
            double amount=double(units)/100000000*(num<0?-1:1);
            a.cash+=amount; a.funding+=amount; day_funding+=amount;
            if(a.entry_time>=0 && funding_times[e]>a.entry_time) a.paid_bps+=rates[e]*10000;
            emit(a,6,e+1,0,-1,a.inventory*p[1],marks[e],amount,0,rates[e]);
            if(a.cash-reserved(a,p)<0 || equity(a,p)<0) {a.error=5; break;}
            risk(a,p); nav_observation(a,p);
        }
        if(a.error) break;
        long long quote=quotes[path*steps+t]; a.price=double(quote)/SCALE;
        if(v[11]==1) {
            if(t>0) {
                if(a.history_count==v[16]+1) {for(int j=1;j<a.history_count;j++) history[j-1]=history[j]; a.history_count--;}
                history[a.history_count++]=a.price;
            }
            a.volatility=volatility(history,a.history_count,p,v);
            a.blocked=p[17]>=0 && a.volatility>p[17]*p[19];
        } else if(v[11]==2) {
            a.funding_bps=funding_signal(a,times[t],funding_times,rates,events,v);
            double spread=(targets[v[0]-1]-targets[0])/targets[0]*10000;
            double margin=spread-2*(p[27]+p[4])*10000-fmax(0.,a.funding_bps)*p[21];
            a.blocked=a.funding_bps>p[20] || margin<p[22];
        }
        if(t==0 && a.state==0) seed(a,step,targets,limits,p,v);
        risk(a,p);
        if(a.state==0 && (a.price<targets[0] || a.price>targets[v[0]-1])) {
            if(v[13] && step-a.last_rebuild>=v[15]) {
                double spacing=-1;
                if(v[12]==2) {spacing=0; for(int j=0;j<a.history_count;j++) spacing+=history[j]; spacing=spacing/v[14]*p[10];}
                rebuild(a,a.price,spacing,step,targets,limits,p,v);
            } else stop(a,v[13]?4:1,v[2]!=0,p);
        }
        for(int j=0;j<a.n;j++) if(a.orders[j].level>=0 && step-a.orders[j].created>=v[1]) cancel_order(a,a.orders[j],p);
        settle(a,step,targets,limits,p,v);
        if(v[11] && a.state==0) {
            if(a.blocked) {for(int j=0;j<a.n;j++) if(a.orders[j].side==0) cancel_order(a,a.orders[j],p);}
            else if(!a.inventory && !a.pending_rebuild) {
                bool active=false; for(int j=0;j<a.n;j++) active|=a.orders[j].status!=0;
                if(!active) seed(a,step,targets,limits,p,v);
            }
            if(v[11]==1 && !a.blocked && v[17] && !a.inventory && !a.pending_rebuild && step-a.last_rebuild>=v[18]) {
                double next=half_width(a.volatility,p);
                double delta=fabs(next-a.half_width)/fmax(1e-9,a.half_width);
                double drift=fabs(a.price-a.center)/fmax(1e-9,a.center);
                if(delta>=p[18] || drift>=.5*a.half_width) {
                    a.half_width=next;
                    rebuild(a,a.price,2*next*a.price,step,targets,limits,p,v);
                }
            }
        }
        if(v[11]==2 && (a.state==0 || a.state==1) && v[21] && a.inventory && a.entry_time>=0) {
            double paid=a.paid_bps;
            double hold=double(times[t]-a.entry_time)/3600000000.; int reason=0;
            if(v[21]==1) {
                if(p[23]>=0 && a.funding_bps>=p[23] && paid>0) reason=6;
                else if(p[23]>=0 && a.funding_bps>=p[23]*2) reason=7;
                else if(p[24]>=0 && hold>=p[24] && a.funding_bps>0) reason=8;
            } else {
                double remaining=(targets[v[0]-1]-targets[0])/targets[0]*10000-2*(p[27]+p[4])*10000-paid-fmax(0.,a.funding_bps)*24;
                if(remaining<=0) reason=9;
                else if(p[24]>=0 && hold>=p[24] && a.funding_bps>0) reason=8;
            }
            if(reason) {stop(a,reason,true,p); settle(a,step,targets,limits,p,v);}
        }
        int capacity=v[7],day_fills=0;
        for(int j=0;j<prior;j++) {
            Order &o=a.orders[j]; if(o.status!=2) continue;
            long long slipped=quote*(SCALE+(o.side==0?v[8]:-v[8]));
            if(o.level>=0 && (o.side==0?slipped>o.limit*SCALE:slipped<o.limit*SCALE)) continue;
            double price=double(slipped)/(double(SCALE)*SCALE);
            int quantity=min(capacity,o.quantity);
            bool liquidation=o.side==1 && o.level<0 && a.state==2;
            if(!liquidation) {
                double remaining=fmax(0.,p[0]*p[7]-a.turnover),units=remaining/(price*p[1]);
                if(units<=quantity && ambiguous(a,remaining,round(units)*price*p[1],p)) break;
                quantity=min(quantity,int(floor(fmin(units,double(quantity)))));
            }
            if(quantity<v[6] || !legal_notional(slipped,quantity,v)) continue;
            double released=o.side==0?quantity*p[1]*o.reservation*(1+p[3]):0;
            double notional=quantity*p[1]*price,fee=notional*p[3];
            if(o.side==0) {
                double available=a.cash-reserved(a,p)+released;
                if(ambiguous(a,notional+fee,available,p)) break;
                if(notional+fee>available) continue;
            }
            int side=o.side,level=o.level,cancel=o.cancel;
            if(side==0) {
                a.entry=(a.inventory*a.entry+quantity*price)/(a.inventory+quantity);
                a.inventory+=quantity; a.cash-=fee;
                if(a.entry_time<0) a.entry_time=times[t];
            } else {
                a.cash+=quantity*p[1]*(price-a.entry)-fee; a.inventory-=quantity;
                if(!a.inventory) {a.entry=0; a.entry_time=-1; a.paid_bps=0;}
            }
            o.quantity-=quantity; if(!o.quantity) o.status=0;
            capacity-=quantity; a.turnover+=notional; a.fills++;
            double slip=quantity*p[1]*fabs(price-a.price);
            day_fee+=fee; day_slip+=slip; a.fees+=fee; a.slip+=slip;
            if(trace) {
                double *f=fills+((path*steps+t)*MAX_ORDERS+day_fills)*8;
                f[0]=o.id; f[1]=side; f[2]=level; f[3]=quantity*p[1]; f[4]=price; f[5]=fee; f[6]=slip; f[7]=level<0?1:0;
            }
            day_fills++; risk(a,p);
            int adjacent=level+(side==0?1:-1);
            if(quantity && a.state==0 && !cancel && !a.pending_rebuild && adjacent>=0 && adjacent<v[0]) {
                int opposite=1-side,key=opposite*MAX_LEVELS+adjacent,proposed=a.n,amount=a.remainder[key]+quantity;
                propose(a,opposite,adjacent,amount,step,targets,limits,p,v); a.remainder[key]=(a.n==proposed)?amount:0;
            }
            settle(a,step,targets,limits,p,v);
        }
        for(int j=0;j<a.n;j++) if(a.orders[j].status && a.orders[j].cancel) {
            Order &o=a.orders[j]; emit(a,3,o.id,o.side,o.level,o.quantity*p[1],double(o.limit)/SCALE);
            o.status=0; risk(a,p); settle(a,step,targets,limits,p,v);
        }
        if(v[12]==2) {
            double high=bars[(path*steps+t)*2],low=bars[(path*steps+t)*2+1];
            double tr=t==0?high-low:fmax(high-low,fmax(fabs(high-previous_close),fabs(low-previous_close)));
            previous_close=a.price;
            if(a.history_count==v[14]) {for(int j=1;j<a.history_count;j++) history[j-1]=history[j]; a.history_count--;}
            history[a.history_count++]=tr;
            if(!a.pending_rebuild && (a.state==0 || a.state==4) && a.history_count==v[14]) {
                double spacing=0; for(int j=0;j<a.history_count;j++) spacing+=history[j]; spacing=spacing/v[14]*p[10];
                bool moved=fabs(a.price-a.center)>=p[11]*spacing;
                if(spacing>0 && (a.state==4 || (moved && step-a.last_rebuild>=v[15])))
                    rebuild(a,a.price,spacing,step,targets,limits,p,v);
            }
        }
        int submitting=a.n;
        for(int j=0;j<submitting;j++) {
            Order &o=a.orders[j]; if(o.status!=1) continue;
            if(o.cancel) {o.status=0; continue;}
            double reservation=fmax(a.price,double(o.limit)/SCALE);
            if(o.side==0) {
                double required=o.quantity*p[1]*reservation*(1+p[3]),exposure=a.inventory*p[1]*a.price;
                for(int k=0;k<a.n;k++) if(a.orders[k].status==2) exposure+=a.orders[k].quantity*p[1]*fmax(a.price,a.orders[k].reservation);
                if(ambiguous(a,required,a.cash-reserved(a,p),p) || ambiguous(a,o.quantity*p[1]*reservation,p[0],p) || ambiguous(a,exposure+o.quantity*p[1]*reservation,p[0],p)) break;
                if(required>a.cash-reserved(a,p) || o.quantity*p[1]*reservation>p[0] || exposure+o.quantity*p[1]*reservation>p[0]) {a.error=2; break;}
            }
            o.reservation=reservation; o.status=2;
            emit(a,4,o.id,o.side,o.level,o.quantity*p[1],double(o.limit)/SCALE,0,reservation);
            risk(a,p); settle(a,step,targets,limits,p,v);
        }
        int count=0; for(int j=0;j<a.n;j++) if(a.orders[j].status) a.orders[count++]=a.orders[j]; a.n=count;
        double nav=equity(a,p),unreal=a.inventory*p[1]*(a.price-a.entry);
        nav_observation(a,p);
        if(trace || t==steps-1) {
            double *out=output+(path*(trace?steps:1)+(trace?t:0))*COLS,committed=reserved(a,p);
            out[0]=a.cash; out[1]=a.inventory*p[1]; out[2]=a.cash-committed; out[3]=committed;
            out[4]=nav-p[0]-unreal+a.fees-a.funding; out[5]=unreal; out[6]=nav; out[7]=day_fee; out[8]=day_slip;
            out[9]=1-nav/a.nav_peak; out[10]=a.fills; out[11]=a.state; out[12]=a.reason;
            out[13]=a.fees; out[14]=a.slip; out[15]=a.max_dd; out[16]=day_funding; out[17]=a.funding;
            out[18]=a.rebuilds; out[19]=a.center; out[20]=a.spacing; out[21]=a.volatility;
            out[22]=a.blocked; out[23]=a.pending_rebuild; out[24]=a.last_rebuild; out[25]=a.funding_bps;
        }
        if(a.error) break;
    }
    errors[path]=a.error;
}
#ifdef GRID_CPU
#ifdef _WIN32
#define GRID_EXPORT __declspec(dllexport)
#else
#define GRID_EXPORT __attribute__((visibility("default")))
#endif
extern "C" GRID_EXPORT void grid_cpu(const long long *quotes,int paths,int steps,const double *targets,
    const long long *limits,const double *p,const long long *v,int trace,double *output,double *fills,int *errors,
    const double *bars,const long long *times,const long long *funding_times,const double *rates,const double *marks,
    const long long *coeff,const double *warmup,double *workspace,int events,int warmup_count,int width,double *audit) {
    for(int path=0;path<paths;path++) grid_path(quotes,path,steps,targets,limits,p,v,trace,output,fills,errors,
        bars,times,funding_times,rates,marks,coeff,warmup,workspace,events,warmup_count,width,audit);
}
#else
extern "C" __global__ void grid_batch(const long long *quotes,int paths,int steps,const double *targets,
    const long long *limits,const double *p,const long long *v,int trace,double *output,double *fills,int *errors,
    const double *bars,const long long *times,const long long *funding_times,const double *rates,const double *marks,
    const long long *coeff,const double *warmup,double *workspace,int events,int warmup_count,int width,double *audit) {
    int path=blockDim.x*blockIdx.x+threadIdx.x;
    if(path<paths) grid_path(quotes,path,steps,targets,limits,p,v,trace,output,fills,errors,
        bars,times,funding_times,rates,marks,coeff,warmup,workspace,events,warmup_count,width,audit);
}
#endif
