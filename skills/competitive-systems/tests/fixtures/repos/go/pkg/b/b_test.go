package b

import "testing"

func TestQuad(t *testing.T) {
	if got := Quad(3); got != "12" {
		t.Fatalf("Quad(3) = %s, want 12", got)
	}
}
