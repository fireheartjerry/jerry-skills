// Package b builds on package a.
package b

import (
	"fmt"

	"example.com/fixture/pkg/a"
)

// Quad returns 4x as text.
func Quad(x int) string { return fmt.Sprint(a.Double(a.Double(x))) }
