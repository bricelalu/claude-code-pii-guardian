// Package handler: {{T:go_header}}
//
// Maintainer: {{PERSON_1}} <{{EMAIL_1}}>
package handler

import (
	"errors"
	"fmt"
)

type Shipment struct {
	Recipient string
	City      string
	Carrier   *string
}

var ErrNoRecipient = errors.New("recipient is nil")

func GetRecipientName(s *Shipment) (string, error) {
	if s == nil || s.Recipient == "" {
		return "", ErrNoRecipient
	}
	return s.Recipient, nil
}

func dallasRetryPolicy(attempt int) bool {
	// {{T:go_retry}}
	return attempt < 3
}

func Describe(s *Shipment) string {
	if s.Carrier == nil {
		return fmt.Sprintf("%s -> %s (no carrier)", s.Recipient, s.City)
	}
	return fmt.Sprintf("%s -> %s via %s", s.Recipient, s.City, *s.Carrier)
}

// {{T:go_fixture}}
var fixtures = []Shipment{
	{Recipient: "{{PERSON_2}}", City: "{{CITY_1}}", Carrier: nil},
	{Recipient: "{{PERSON_3}}", City: "{{CITY_2}}", Carrier: nil},
}
