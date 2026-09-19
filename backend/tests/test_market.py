from __future__ import annotations

from kotsin_crypto.venue.delta.market import chain_summary, normalize_option, normalize_perp


def test_perp_normalization_derives_basis_and_annualized_funding() -> None:
    row = normalize_perp(
        {
            "symbol": "BTCUSD",
            "product_id": 27,
            "mark_price": "81000",
            "spot_price": "81081",
            "close": 80990.5,
            "funding_rate": "0.01",
            "oi_value_usd": "1000",
            "turnover_usd": 5.0,
            "quotes": {"best_bid": "80999", "best_ask": "81001"},
            "price_band": {"lower_limit": "77000", "upper_limit": "85000"},
            "mark_change_24h": "0.5",
        }
    )
    assert row["symbol"] == "BTCUSD" and row["funding_pct"] == 0.01
    assert abs(row["funding_annualized_pct"] - 10.95) < 1e-9
    assert abs(row["basis_bps"] - (81000 - 81081) / 81081 * 1e4) < 1e-9
    assert row["bid"] == 80999.0 and row["band_hi"] == 85000.0


def test_option_normalization_and_chain_summary() -> None:
    rows = [
        normalize_option(
            {
                "symbol": "C-BTC-80000-210926",
                "contract_type": "call_options",
                "strike_price": "80000",
                "mark_price": "1500",
                "oi_contracts": "100",
                "quotes": {"best_bid": "1490", "best_ask": "1510", "mark_iv": "0.55"},
                "greeks": {"delta": "0.6"},
                "spot_price": "81000",
            }
        ),
        normalize_option(
            {
                "symbol": "P-BTC-80000-210926",
                "contract_type": "put_options",
                "strike_price": "80000",
                "mark_price": "500",
                "oi_contracts": "300",
                "quotes": {"mark_iv": "0.57"},
                "greeks": {"delta": "-0.4"},
                "spot_price": "81000",
            }
        ),
        normalize_option(
            {
                "symbol": "C-BTC-84000-210926",
                "contract_type": "call_options",
                "strike_price": "84000",
                "mark_price": "200",
                "oi_contracts": "50",
                "quotes": {"mark_iv": "0.60"},
                "greeks": {},
                "spot_price": "81000",
            }
        ),
    ]
    assert (
        rows[0]["type"] == "C"
        and rows[1]["type"] == "P"
        and rows[0]["iv"] == 0.55
        and rows[0]["delta"] == 0.6
    )
    s = chain_summary(rows, 81000.0)
    assert s["call_oi_contracts"] == 150 and s["put_oi_contracts"] == 300 and s["pcr_oi"] == 2.0
    # pain at 80000: calls 0 + puts 0 = 0; at 84000: 100*4000 = 400000 → max pain 80000
    assert s["max_pain"] == 80000.0
    assert abs(s["atm_iv"] - 0.56) < 1e-9
