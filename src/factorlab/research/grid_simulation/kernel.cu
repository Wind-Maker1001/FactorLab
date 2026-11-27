// USDM long-only close-quote research economics; no authoritative posting.
#ifdef GRID_CPU
#include <cmath>
#include <algorithm>
using std::min;
#define __device__
static unsigned long long __umul64hi(unsigned long long a, unsigned long long b) {
    unsigned long long a0=static_cast<unsigned int>(a), a1=a>>32;
    unsigned long long b0=static_cast<unsigned int>(b), b1=b>>32;
    unsigned long long t=a0*b0;
    unsigned long long k=t>>32;
    t=a1*b0+k;
    unsigned long long w1=static_cast<unsigned int>(t), w2=t>>32;
    t=a0*b1+w1;
    return a1*b1+w2+(t>>32);
}
#endif
#define MAX_ORDERS 128
#define MAX_LEVELS 33
#define SCALE 1000000LL
#define COLS 16
struct Order {
    long long limit;
    int quantity, side, level, created, status, cancel, id;
    double reservation;
};
struct Account {
    Order orders[MAX_ORDERS];
    int n, inventory, state, reason, fills, error, issued;
    int remainder[2 * MAX_LEVELS];
    double cash, entry, price, peak, nav_peak, turnover, fees, slip, max_dd;
};
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
    return a.cash + a.inventory * p[1] * (a.price - a.entry);
}
__device__ double reserved(Account &a, const double *p) {
    double value = a.inventory * p[1] * a.entry;
    for (int j=0; j<a.n; j++) {
        Order &o=a.orders[j];
        if (o.status==2 && o.side==0)
            value += o.quantity * p[1] * o.reservation * (1+p[3]);
    }
    return value;
}
__device__ void risk(Account &a, const double *p) {
    double nav=equity(a,p);
    a.peak=fmax(a.peak,nav);
    if (a.state!=0) return;
    if(ambiguous(a,nav,a.peak*(1-p[6]),p) || ambiguous(a,a.turnover,p[0]*p[7],p)) return;
    int reason=0;
    if (1-nav/a.peak >= p[6]) reason=2;
    else if (a.turnover/p[0] >= p[7]) reason=3;
    if (reason) {
        a.state=1; a.reason=reason;
        for(int j=0;j<a.n;j++) a.orders[j].cancel=1;
        for(int j=0;j<2*MAX_LEVELS;j++) a.remainder[j]=0;
    }
}
__device__ void propose(Account &a, int side, int level, int quantity, int step,
                        const double *targets, const long long *limits,
                        const double *p, const long long *v, bool market=false) {
    if (!(a.state==0 || (a.state==2 && market && side==1))) return;
    if(quantity < v[6] || quantity<=0) return;
    double target=market?a.price:targets[level];
    long long limit=market?0:limits[side*v[0]+level];
    double price=market?a.price:double(limit)/SCALE;
    long long numerator=market?static_cast<long long>(llround(a.price*SCALE))*SCALE:limit*SCALE;
    if ((!market && price<p[8]) || !legal_notional(numerator,quantity,v)) return;
    int buys=0,sells=0;
    double pending=0;
    for(int j=0;j<a.n;j++) {
        Order &o=a.orders[j];
        if (!o.status) continue;
        if(o.side==0) {
            buys+=o.quantity;
            if(o.status==1) pending+=o.quantity*p[1]*fmax(a.price,double(o.limit)/SCALE)*(1+p[3]+p[4]);
        } else sells+=o.quantity;
    }
    double cost=quantity*p[1]*price*(1+p[3]+p[4]);
    double committed=reserved(a,p);
    if(side==1) { if(quantity>a.inventory-sells) return; }
    else {
        if(a.inventory+buys+quantity>v[5]) return;
        if(ambiguous(a,pending+cost,a.cash-committed,p) ||
           ambiguous(a,committed+pending+cost,p[5],p)) return;
        if(pending+cost>a.cash-committed || committed+pending+cost>p[5]) return;
    }
    int adjacent=level+(side==0?1:-1);
    if(!market && adjacent>=0 && adjacent<v[0]) {
        // quantity_unit cancels from both sides of the paired-cost test.
        double other=targets[adjacent];
        if(ambiguous(a,fabs(other-target),(price+other)*(p[3]+p[4]),p)) return;
        if(fabs(other-target) <= (price+other)*(p[3]+p[4])) return;
    }
    if(a.n==MAX_ORDERS) {a.error=1; return;}
    Order o;
    o.limit=limit; o.quantity=quantity; o.side=side; o.level=market?-1:level;
    o.created=step; o.status=1; o.cancel=0; o.reservation=0; o.id=++a.issued;
    a.orders[a.n++]=o;
}
__device__ void settle(Account &a, int step, const double *targets,
                       const long long *limits, const double *p, const long long *v) {
    for(int j=0;j<a.n;j++) if(a.orders[j].status) return;
    if(a.state==2) {
        if(a.inventory) propose(a,1,-1,a.inventory,step,targets,limits,p,v,true);
        else a.state=3;
    }
}
__device__ void grid_path(const long long *quotes, int path, int steps,
    const double *targets, const long long *limits, const double *p, const long long *v,
    int trace, double *output, double *fills, int *errors) {
    Account a;
    a.n=0; a.inventory=v[4]; a.state=0; a.reason=0; a.fills=0; a.error=0; a.issued=0;
    a.cash=p[0]; a.entry=double(quotes[path*steps])/SCALE;
    a.price=a.entry; a.peak=p[0]; a.nav_peak=p[0];
    a.turnover=0; a.fees=0; a.slip=0; a.max_dd=0;
    for(int j=0;j<2*MAX_LEVELS;j++) a.remainder[j]=0;
    for(int t=0;t<steps;t++) {
        int step=t+1;
        long long quote=quotes[path*steps+t];
        a.price=double(quote)/SCALE;
        int prior=a.n;
        double day_fee=0,day_slip=0;
        if(t==0) {
            for(int level=0;level<v[0];level++)
                if(targets[level]!=a.price)
                    propose(a,targets[level]<a.price?0:1,level,v[3],step,targets,limits,p,v);
        }
        risk(a,p);
        if(a.state==0 && (a.price<targets[0] || a.price>targets[v[0]-1])) {
            a.state=(v[2] && a.inventory)?2:1;
            a.reason=1;
            for(int j=0;j<a.n;j++) a.orders[j].cancel=1;
            for(int j=0;j<2*MAX_LEVELS;j++) a.remainder[j]=0;
        }
        for(int j=0;j<a.n;j++) {
            Order &o=a.orders[j];
            if(o.level>=0 && step-o.created>=v[1]) o.cancel=1;
        }
        settle(a,step,targets,limits,p,v);
        int capacity=v[7], day_fills=0;
        for(int j=0;j<prior;j++) {
            Order &o=a.orders[j];
            if(o.status!=2) continue;
            long long slipped=quote*(SCALE+(o.side==0?v[8]:-v[8]));
            if(o.level>=0 && (o.side==0?slipped>o.limit*SCALE:slipped<o.limit*SCALE)) continue;
            double price=double(slipped)/(double(SCALE)*SCALE);
            int quantity=min(capacity,o.quantity);
            bool liquidation=o.side==1 && o.level<0 && a.state==2;
            if(!liquidation) {
                double remaining=fmax(0.,p[0]*p[7]-a.turnover);
                double units=remaining/(price*p[1]);
                if(units<=quantity && ambiguous(a,remaining,round(units)*price*p[1],p)) break;
                quantity=min(quantity,int(floor(fmin(units,double(quantity)))));
            }
            if(quantity<v[6] || !legal_notional(slipped,quantity,v)) continue;
            double released=o.side==0?quantity*p[1]*o.reservation*(1+p[3]):0;
            double notional=quantity*p[1]*price, fee=notional*p[3];
            if(o.side==0) {
                double available=a.cash-reserved(a,p)+released;
                if(ambiguous(a,notional+fee,available,p)) break;
                if(notional+fee>available) continue;
            }
            int side=o.side, level=o.level, cancel=o.cancel;
            if(side==0) {
                a.entry=(a.inventory*a.entry+quantity*price)/(a.inventory+quantity);
                a.inventory+=quantity; a.cash-=fee;
            } else {
                a.cash+=quantity*p[1]*(price-a.entry)-fee;
                a.inventory-=quantity;
                if(!a.inventory) a.entry=0;
            }
            o.quantity-=quantity;
            if(!o.quantity) o.status=0;
            capacity-=quantity; a.turnover+=notional; a.fills++;
            double slip=quantity*p[1]*fabs(price-a.price);
            day_fee+=fee; day_slip+=slip; a.fees+=fee; a.slip+=slip;
            if(trace) {
                double *f=fills+((path*steps+t)*MAX_ORDERS+day_fills)*8;
                f[0]=o.id; f[1]=side; f[2]=level; f[3]=quantity*p[1];
                f[4]=price; f[5]=fee; f[6]=slip; f[7]=level<0?1:0;
            }
            day_fills++;
            risk(a,p);
            int adjacent=level+(side==0?1:-1);
            if(quantity && a.state==0 && !cancel && adjacent>=0 && adjacent<v[0]) {
                int opposite=1-side, key=opposite*MAX_LEVELS+adjacent;
                int proposed=a.n;
                int amount=a.remainder[key]+quantity;
                propose(a,opposite,adjacent,amount,step,targets,limits,p,v);
                a.remainder[key]=(a.n==proposed)?amount:0;
            }
            settle(a,step,targets,limits,p,v);
        }
        // Cancellations are confirmed in submission order, before new submissions.
        for(int j=0;j<a.n;j++) {
            if(a.orders[j].status && a.orders[j].cancel) {
                a.orders[j].status=0;
                risk(a,p);
                settle(a,step,targets,limits,p,v);
            }
        }
        int submitting=a.n;
        for(int j=0;j<submitting;j++) {
            Order &o=a.orders[j];
            if(o.status!=1) continue;
            if(o.cancel) {o.status=0; continue;}
            double reservation=fmax(a.price,double(o.limit)/SCALE);
            if(o.side==0) {
                double required=o.quantity*p[1]*reservation*(1+p[3]);
                double exposure=a.inventory*p[1]*a.price;
                for(int k=0;k<a.n;k++) if(a.orders[k].status==2)
                    exposure+=a.orders[k].quantity*p[1]*fmax(a.price,a.orders[k].reservation);
                if(ambiguous(a,required,a.cash-reserved(a,p),p) ||
                   ambiguous(a,o.quantity*p[1]*reservation,p[0],p) ||
                   ambiguous(a,exposure+o.quantity*p[1]*reservation,p[0],p)) break;
                if(required>a.cash-reserved(a,p) || o.quantity*p[1]*reservation>p[0] ||
                   exposure+o.quantity*p[1]*reservation>p[0]) {a.error=2; break;}
            }
            o.reservation=reservation; o.status=2;
            risk(a,p);
            settle(a,step,targets,limits,p,v);
        }
        int count=0;
        for(int j=0;j<a.n;j++) if(a.orders[j].status) a.orders[count++]=a.orders[j];
        a.n=count;
        double nav=equity(a,p), unreal=a.inventory*p[1]*(a.price-a.entry);
        a.nav_peak=fmax(a.nav_peak,nav);
        double dd=1-nav/a.nav_peak;
        a.max_dd=fmax(a.max_dd,dd);
        if(trace || t==steps-1) {
            double *out=output+(path*(trace?steps:1)+(trace?t:0))*COLS;
            double committed=reserved(a,p);
            out[0]=a.cash; out[1]=a.inventory*p[1]; out[2]=a.cash-committed;
            out[3]=committed; out[4]=nav-p[0]-unreal+a.fees;
            out[5]=unreal; out[6]=nav; out[7]=day_fee; out[8]=day_slip;
            out[9]=dd; out[10]=a.fills; out[11]=a.state; out[12]=a.reason;
            out[13]=a.fees; out[14]=a.slip; out[15]=a.max_dd;
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
extern "C" GRID_EXPORT void grid_cpu(const long long *quotes, int paths, int steps,
    const double *targets, const long long *limits, const double *p, const long long *v,
    int trace, double *output, double *fills, int *errors) {
    for(int path=0;path<paths;path++)
        grid_path(quotes,path,steps,targets,limits,p,v,trace,output,fills,errors);
}
#else
extern "C" __global__ void grid_batch(const long long *quotes, int paths, int steps,
    const double *targets, const long long *limits, const double *p, const long long *v,
    int trace, double *output, double *fills, int *errors) {
    int path=blockDim.x*blockIdx.x+threadIdx.x;
    if(path<paths) grid_path(quotes,path,steps,targets,limits,p,v,trace,output,fills,errors);
}
#endif
