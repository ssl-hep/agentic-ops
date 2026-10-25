package authz

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.temporal.io/server/common/authorization"
	"google.golang.org/grpc/credentials"
)

// testPKI is the certificate scheme in miniature: a root that signs admin, controller and
// reader certificates and the worker CAs, which sign worker certificates. otherCA is an
// intermediate under the same root that is not a worker CA.
type testPKI struct {
	root, workerCA, nextWorkerCA, otherCA *x509.Certificate
	keys                                  map[*x509.Certificate]crypto.Signer
}

func newTestPKI(t *testing.T) *testPKI {
	t.Helper()
	p := &testPKI{keys: map[*x509.Certificate]crypto.Signer{}}
	p.root = p.make(t, nil, pkix.Name{Organization: []string{Organization}, CommonName: "test root"}, true)
	p.workerCA = p.make(t, p.root, pkix.Name{Organization: []string{Organization}, OrganizationalUnit: []string{"worker-ca"}, CommonName: "worker CA 1"}, true)
	p.nextWorkerCA = p.make(t, p.root, pkix.Name{Organization: []string{Organization}, OrganizationalUnit: []string{"worker-ca"}, CommonName: "worker CA 2"}, true)
	p.otherCA = p.make(t, p.root, pkix.Name{Organization: []string{Organization}, CommonName: "some other intermediate"}, true)
	return p
}

func (p *testPKI) make(t *testing.T, parent *x509.Certificate, subject pkix.Name, isCA bool) *x509.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial, _ := rand.Int(rand.Reader, big.NewInt(1<<62))
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               subject,
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(time.Hour),
		BasicConstraintsValid: true,
		IsCA:                  isCA,
	}
	if isCA {
		tmpl.KeyUsage = x509.KeyUsageCertSign
	} else {
		tmpl.KeyUsage = x509.KeyUsageDigitalSignature
		tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	}
	signer, issuer := crypto.Signer(key), tmpl
	if parent != nil {
		signer, issuer = p.keys[parent], parent
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, issuer, key.Public(), signer)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	p.keys[cert] = key
	return cert
}

// authInfo issues a client certificate under parent and verifies it the way the frontend's
// TLS handshake does: against the trusted roots, with the intermediates the client sent.
func (p *testPKI) authInfo(t *testing.T, parent *x509.Certificate, subject pkix.Name, roots ...*x509.Certificate) *authorization.AuthInfo {
	t.Helper()
	leaf := p.make(t, parent, subject, false)
	if len(roots) == 0 {
		roots = []*x509.Certificate{p.root}
	}
	rootPool, intermediates := x509.NewCertPool(), x509.NewCertPool()
	for _, r := range roots {
		rootPool.AddCert(r)
	}
	if parent != p.root {
		intermediates.AddCert(parent)
	}
	chains, err := leaf.Verify(x509.VerifyOptions{Roots: rootPool, Intermediates: intermediates, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
	if err != nil {
		t.Fatalf("verifying %v: %v", subject, err)
	}
	return &authorization.AuthInfo{
		TLSSubject:    &leaf.Subject,
		TLSConnection: &credentials.TLSInfo{State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf}, VerifiedChains: chains}},
	}
}

func subject(role, cn string) pkix.Name {
	return pkix.Name{Organization: []string{Organization}, OrganizationalUnit: []string{role}, CommonName: cn}
}

func (p *testPKI) mapper() *ClaimMapper {
	return NewClaimMapper(ns, []*x509.Certificate{p.workerCA, p.nextWorkerCA})
}

// claims issues a certificate from the CA its role belongs to and maps it.
func (p *testPKI) claims(t *testing.T, role, cn string) *authorization.Claims {
	t.Helper()
	parent := p.root
	if role == RoleWorker {
		parent = p.workerCA
	}
	claims, err := p.mapper().GetClaims(p.authInfo(t, parent, subject(role, cn)))
	if err != nil {
		t.Fatalf("GetClaims(%s, %s): %v", role, cn, err)
	}
	return claims
}

func TestClaimMapperAccepts(t *testing.T) {
	p := newTestPKI(t)
	m := p.mapper()

	if c := p.claims(t, RoleAdmin, "ben"); c.System != authorization.RoleAdmin {
		t.Errorf("admin: system role %v", c.System)
	}
	if c := p.claims(t, RoleController, "controller"); c.System != authorization.RoleUndefined || c.Namespaces[ns] != authorization.RoleWriter || len(c.Namespaces) != 1 {
		t.Errorf("controller: %+v", c)
	}
	if c := p.claims(t, RoleReader, "web-ui"); c.System != authorization.RoleUndefined || c.Namespaces[ns] != authorization.RoleReader || len(c.Namespaces) != 1 {
		t.Errorf("reader: %+v", c)
	}
	unit := p.claims(t, RoleWorker, "ewkit-phase0")
	if scope, ok := unit.Extensions.(*WorkerScope); !ok || scope.Production != "phase0" || unit.Namespaces[ns] != authorization.RoleWorker {
		t.Errorf("unit worker: %+v", unit)
	}
	if scope, ok := p.claims(t, RoleWorker, AfIoName).Extensions.(*WorkerScope); !ok || scope.Production != "" {
		t.Errorf("af-io: %+v", scope)
	}
	// During a rotation both worker CAs are configured
	if _, err := m.GetClaims(p.authInfo(t, p.nextWorkerCA, subject(RoleWorker, "ewkit-phase0"))); err != nil {
		t.Errorf("worker from the next worker CA: %v", err)
	}
}

func TestClaimMapperRejects(t *testing.T) {
	p := newTestPKI(t)
	m := p.mapper()
	root, workerCA, otherCA := p.root, p.workerCA, p.otherCA
	cases := map[string]*authorization.AuthInfo{
		"no certificate":     nil,
		"no verified chain":  {TLSSubject: &pkix.Name{Organization: []string{Organization}, OrganizationalUnit: []string{RoleAdmin}}},
		"wrong organization": p.authInfo(t, root, pkix.Name{Organization: []string{"cern"}, OrganizationalUnit: []string{RoleAdmin}}),
		"two roles":          p.authInfo(t, root, pkix.Name{Organization: []string{Organization}, OrganizationalUnit: []string{RoleReader, RoleAdmin}}),
		"unknown role":       p.authInfo(t, root, subject("operator", "x")),

		"worker without scope": p.authInfo(t, workerCA, subject(RoleWorker, "someone")),
		"production with dash": p.authInfo(t, workerCA, subject(RoleWorker, "ewkit-phase0-8g")),
		"empty production":     p.authInfo(t, workerCA, subject(RoleWorker, "ewkit-")),

		// Roles checked against the issuing CA
		"worker signed by the root":            p.authInfo(t, root, subject(RoleWorker, "ewkit-phase0")),
		"af-io signed by the root":             p.authInfo(t, root, subject(RoleWorker, AfIoName)),
		"worker from another intermediate":     p.authInfo(t, otherCA, subject(RoleWorker, "ewkit-phase0")),
		"admin from the worker CA":             p.authInfo(t, workerCA, subject(RoleAdmin, "admin")),
		"controller from the worker CA":        p.authInfo(t, workerCA, subject(RoleController, "controller")),
		"reader from the worker CA":            p.authInfo(t, workerCA, subject(RoleReader, "web-ui")),
		"controller from another intermediate": p.authInfo(t, otherCA, subject(RoleController, "controller")),
		// A misconfigured server that also trusts the worker CA as a root
		"controller from the worker CA as a root": p.authInfo(t, workerCA, subject(RoleController, "controller"), root, workerCA),
	}
	for name, info := range cases {
		t.Run(name, func(t *testing.T) {
			if claims, err := m.GetClaims(info); err == nil {
				t.Errorf("expected an error, got %+v", claims)
			}
		})
	}

	t.Run("no worker CAs configured", func(t *testing.T) {
		info := p.authInfo(t, workerCA, subject(RoleWorker, "ewkit-phase0"))
		if _, err := NewClaimMapper(ns, nil).GetClaims(info); err == nil {
			t.Error("expected an error")
		}
	})
}

func TestLoadCertificates(t *testing.T) {
	p := newTestPKI(t)
	dir := t.TempDir()
	var data []byte
	for _, c := range []*x509.Certificate{p.workerCA, p.nextWorkerCA} {
		data = append(data, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.Raw})...)
	}
	path := filepath.Join(dir, "worker-ca.pem")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	certs, err := LoadCertificates(path)
	if err != nil || len(certs) != 2 || !certs[1].Equal(p.nextWorkerCA) {
		t.Fatalf("LoadCertificates = %d certificates, %v", len(certs), err)
	}

	empty := filepath.Join(dir, "empty.pem")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadCertificates(empty); err == nil {
		t.Error("an empty file should be an error")
	}
}
