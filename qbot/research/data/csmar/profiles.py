"""Code-owned native contracts; dictionary digests are versions, not vintages."""

from qbot.research.data.evidence import json_bytes, sha256

VERSION = "csmar-native-candidate-v2-exact-decimal-binary64"
SOURCE_ID = "CSMAR_ZIP_REFERENCE_V1"
SYNTHETIC_DICTIONARY = b"SYNTHETIC SOFTWARE FIXTURE ONLY; NO FINANCIAL QUALIFICATION\n"

# Exact upstream DES bytes reviewed with the 20261021 full audit. Unknown
# dictionary versions require review/code changes, never field-name guessing.
PROFILES = {
    "TRDNEW_Dalyr": ("12b4277c15ae5fa2f6b329480ff9e5df5cfe4903208bc84871751e5c29ad6a98", "Stkcd", "Trddt"),
    "IDX_Idxtrd": ("c5ab3870f72ca2ca1740b85477c9e15276f379774b0ecb1f3ac62a343c340e34", "Indexcd", "Idxtrd01"),
    "TRD_AdjustFactor": ("9b6559259c6ad56b96074da198da3a003f7624f4056842158f3248454b5d532a", "Symbol", "TradingDate"),
    "TRDNEW_Co": ("7b1bb88ef3c02f9075f57095dfe343882b30f9a426a7bf39611f7ae0a677093d", "Stkcd", "Listdt"),
    "TRDNEW_Cptl": ("5b05b41e3a9a5b9ce3a80943c866741e09f98a993d7a9bb166880e663d353020", "Stkcd", "Exdistdt"),
    "TRDNEW_Capchg": ("17114ff76bba86fde94d937d735810dbd4e7e13f19ad582e00bd0ffacdeef9c4", "Stkcd", "Shrchgdt"),
    "IDX_Chgsmp": ("7e761223a49aa9f34887b5472f4a5e3a6a787686c637406da2bc25edb2b2ec82", "Indexcd", "Chgsmp01"),
    "IDX_Smprat": ("e1cb312e7421a2f4ef8c2a830b128744541b4d45cc16d032681f78c1cdffe236", "Indexcd", "Enddt"),
}
MAPPINGS = {
    "TRDNEW_Dalyr": {
        "Opnprc": ("open", "元/股", "CNY/share", "1"),
        "Hiprc": ("high", "元/股", "CNY/share", "1"),
        "Loprc": ("low", "元/股", "CNY/share", "1"),
        "Clsprc": ("close", "元/股", "CNY/share", "1"),
        "Dnshrtrd": ("volume", "股", "shares", "1"),
        "Dnvaltrd": ("amount", "元", "CNY", "1"),
    },
    "IDX_Idxtrd": {
        "Idxtrd02": ("open", "没有单位", "index_points", "1"),
        "Idxtrd03": ("high", "没有单位", "index_points", "1"),
        "Idxtrd04": ("low", "没有单位", "index_points", "1"),
        "Idxtrd05": ("close", "没有单位", "index_points", "1"),
        "Idxtrd06": ("volume", "万股", "shares", "10000"),
        "Idxtrd07": ("amount", "万元", "CNY", "10000"),
    },
}
# Markettype is explicitly defined in DES. Do not apply ticker-prefix guesses.
MARKETS = {"1": "SH", "32": "SH", "4": "SZ", "16": "SZ"}
PRIMARY_KEYS = {
    "TRDNEW_Dalyr": ("Stkcd", "Trddt"),
    "IDX_Idxtrd": ("Indexcd", "Idxtrd01"),
    "TRD_AdjustFactor": ("Symbol", "TradingDate"),
    "TRDNEW_Co": ("Stkcd",),
    "TRDNEW_Cptl": ("Stkcd", "Disttyp", "Exdistdt", "Markettype"),
    "TRDNEW_Capchg": ("Stkcd", "Shrchgdt", "Shrtyp"),
    "IDX_Chgsmp": ("Indexcd", "Chgsmp01", "Chgsmp02", "Chgsmp04"),
    "IDX_Smprat": ("Indexcd", "Enddt", "Stkcd"),
}


def profile_sha256():
    return sha256(json_bytes([VERSION, PROFILES, MAPPINGS, MARKETS, PRIMARY_KEYS]))
