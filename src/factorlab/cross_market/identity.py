"""Bitemporal listing identities and symbol aliases with explicit source evidence."""
import pandas as pd
from .contracts import ContractError, session_date, instant

REQUIRED = {'instrument_id', 'symbol', 'market', 'exchange', 'asset_class', 'currency',
            'security_type', 'listing_date', 'delisting_date', 'valid_from', 'valid_to',
            'available_at', 'source_identity', 'evidence'}


class IdentityRegistry:
    """Half-open aliases [valid_from, valid_to); delisting date is last listed day.

    instrument_id is supplied from source-backed listing episodes, never inferred
    from a current list. Evidence authenticity remains an external qualification.
    """
    def __init__(self, aliases):
        if missing := REQUIRED - set(aliases):
            raise ContractError('identity fields missing: ' + str(sorted(missing)))
        frame = aliases.copy()
        for col in ('instrument_id', 'symbol', 'market', 'exchange', 'asset_class', 'currency', 'source_identity', 'evidence', 'security_type'):
            if frame[col].isna().any() or frame[col].astype(str).str.strip().eq('').any():
                raise ContractError('identity evidence/field missing: ' + col)
        for col in ('listing_date', 'delisting_date', 'valid_from', 'valid_to'):
            frame[col] = pd.to_datetime(frame[col])
            for value in frame[col].dropna():
                session_date(value)
        if frame[['listing_date', 'valid_from']].isna().any().any():
            raise ContractError('listing and alias start required')
        frame['available_at'] = pd.to_datetime([instant(x) for x in frame.available_at], utc=True)
        if (frame.valid_to.notna() & (frame.valid_to <= frame.valid_from)).any():
            raise ContractError('empty/reversed alias interval')
        if (frame.delisting_date.notna() & (frame.delisting_date < frame.listing_date)).any():
            raise ContractError('delisting precedes listing')
        for key in ('instrument_id',):
            stable = frame.groupby(key)[['market', 'currency', 'asset_class', 'listing_date', 'delisting_date']].nunique(dropna=False)
            if stable.gt(1).any().any():
                raise ContractError('inconsistent stable identity')
        for keys in (['market', 'exchange', 'symbol'], ['instrument_id']):
            for _, group in frame.groupby(keys, dropna=False):
                group = group.sort_values('valid_from')
                ends = group.valid_to.fillna(pd.Timestamp.max).to_numpy()
                if len(group) > 1 and (group.valid_from.to_numpy()[1:] < ends[:-1]).any():
                    raise ContractError('overlapping symbol/identity aliases')
        self.aliases = frame.reset_index(drop=True)

    def active(self, date, as_of):
        day, cutoff = session_date(date), instant(as_of)
        f = self.aliases
        return f.loc[(f.valid_from <= day) & (f.valid_to.isna() | (day < f.valid_to)) &
                     (f.listing_date <= day) & (f.delisting_date.isna() | (day <= f.delisting_date)) &
                     (f.available_at <= cutoff)].copy()

    def resolve(self, symbol, market, exchange, date, as_of):
        f = self.active(date, as_of)
        f = f.loc[(f.symbol == symbol) & (f.market == market) & (f.exchange == exchange)]
        if len(f) != 1:
            raise ContractError('unresolved or ambiguous historical identity')
        return f.iloc[0].instrument_id

    def legacy_cn(self, symbol, date, as_of):
        from factorlab.data.schema import normalize_symbol
        symbol = normalize_symbol(symbol)
        return self.resolve(symbol, 'CN', {'SH': 'XSHG', 'SZ': 'XSHE'}[symbol[-2:]], date, as_of)


class DynamicUniverse:
    def __init__(self, identities, market, name, membership=None):
        self.identities, self.market, self.name = identities, market, name
        self.membership = membership
        if name in {'CSI500', 'US_INDEX'} and membership is None:
            raise ContractError('historical index membership required')

    def members(self, date, as_of):
        f = self.identities.active(date, as_of)
        f = f.loc[(f.market == self.market) & (f.asset_class == 'equity') & (f.security_type == 'common_stock')]
        f = f.loc[f.exchange.isin({'CN': ['XSHG', 'XSHE'], 'US': ['XNYS', 'XNAS']}[self.market])]
        if self.membership is not None:
            m = self.membership
            m = m.loc[(pd.to_datetime(m.date) == session_date(date)) &
                      (pd.to_datetime(m.available_at, utc=True) <= instant(as_of))]
            if m.instrument_id.duplicated().any():
                raise ContractError('duplicate index constituents')
            if self.name == 'CSI500' and len(m) != 500:
                raise ContractError('CSI500 still requires exactly 500 members')
            f = f.loc[f.instrument_id.isin(m.instrument_id)]
            if len(f) != len(m):
                raise ContractError('index identity evidence incomplete')
        return f.sort_values('instrument_id').reset_index(drop=True)
