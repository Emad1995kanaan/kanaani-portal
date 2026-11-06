from tests.conftest import page_csrf, signed_in


def test_reseller_cannot_open_admin(reseller):
    c = signed_in("seller1")
    assert c.get("/admin").status_code == 403
    assert c.get("/api/v1/admin/stats").status_code == 403


def test_admin_has_no_reseller_pages(admin):
    c = signed_in("boss")
    assert c.get("/r").status_code == 403


def test_anonymous_is_sent_to_login(client):
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/v1/me").status_code == 401


def test_reseller_cannot_see_or_touch_other_resellers_customers(reseller, reseller2, plan1):
    c1 = signed_in("seller1")
    tok = page_csrf(c1, "/r/customers")
    r = c1.post("/r/customers", data={"csrf_token": tok, "full_name": "Only Mine", "plan_id": plan1.id}, follow_redirects=False)
    cid = int(r.headers["location"].split("/")[-1].split("?")[0])
    c2 = signed_in("seller2")
    assert c2.get(f"/r/customers/{cid}").status_code == 404
    tok2 = page_csrf(c2, "/r/customers")
    assert c2.post(f"/r/customers/{cid}/renew", data={"csrf_token": tok2, "plan_id": plan1.id}).status_code == 404
    assert "Only Mine" not in c2.get("/r/customers").text
    assert c2.get(f"/api/v1/customers/{cid}").status_code == 404
    assert c2.get("/api/v1/customers").json()["total"] == 0


def test_form_posts_need_csrf(admin, reseller):
    c = signed_in("boss")
    assert c.post(f"/admin/resellers/{reseller.id}/credits", data={"op": "allocate", "amount": "5"}).status_code == 403
    assert c.post(f"/admin/resellers/{reseller.id}/credits", data={"op": "allocate", "amount": "5", "csrf_token": "wrong"}).status_code == 403


def test_api_writes_need_csrf_header(reseller, plan1):
    c = signed_in("seller1")
    assert c.post("/api/v1/customers", json={"full_name": "Api Person", "plan_id": plan1.id}).status_code == 403
    tok = c.get("/api/v1/me").json()["csrf_token"]
    r = c.post("/api/v1/customers", json={"full_name": "Api Person", "plan_id": plan1.id}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 201


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_html_is_escaped(reseller, plan1):
    c = signed_in("seller1")
    tok = page_csrf(c, "/r/customers")
    c.post("/r/customers", data={"csrf_token": tok, "full_name": "<script>alert(1)</script>", "plan_id": plan1.id})
    html = c.get("/r/customers").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
