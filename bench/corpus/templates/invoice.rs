//! {{T:rs_header}}
//!
//! Maintainer: {{PERSON_1}} <{{EMAIL_1}}>

use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct Invoice {
    pub customer: String,
    pub city: String,
    pub total_cents: u64,
    pub po_number: Option<String>,
}

pub fn get_customer_name(inv: Option<&Invoice>) -> Option<&str> {
    match inv {
        Some(i) => Some(i.customer.as_str()),
        None => None,
    }
}

pub fn sydney_tax_rate(city: &str) -> f64 {
    // {{T:rs_tax}}
    if city.is_empty() { 0.0 } else { 0.2 }
}

pub fn seed() -> HashMap<u32, Invoice> {
    let mut m = HashMap::new();
    // {{T:rs_seed}}
    m.insert(1, Invoice { customer: "{{PERSON_2}}".into(), city: "{{CITY_1}}".into(), total_cents: 12_000, po_number: None });
    m.insert(2, Invoice { customer: "{{PERSON_3}}".into(), city: "{{CITY_2}}".into(), total_cents: 4_550, po_number: Some("PO-77".into()) });
    m
}
