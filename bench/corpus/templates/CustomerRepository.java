package com.acme.crm;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

/**
 * {{T:java_header}}
 *
 * @author {{PERSON_1}} <{{EMAIL_1}}>
 */
public class CustomerRepository {

    public static final class Customer {
        private final String id;
        private final String fullName;
        private final String city;

        public Customer(String id, String fullName, String city) {
            this.id = id;
            this.fullName = fullName;
            this.city = city;
        }

        public String id() { return id; }
        public String fullName() { return fullName; }
        public String city() { return city; }
    }

    private final Map<String, Customer> store = new HashMap<>();

    public CustomerRepository() {
        // {{T:java_seed}}
        store.put("c-1", new Customer("c-1", "{{PERSON_2}}", "{{CITY_1}}"));
        store.put("c-2", new Customer("c-2", "{{PERSON_3}}", "{{CITY_2}}"));
    }

    public Optional<String> getCustomerName(String id) {
        Customer c = store.get(id);
        if (c == null) {
            return Optional.empty();
        }
        return Optional.of(c.fullName());
    }

    public String findByCity(String cityName) {
        // {{T:java_lookup}}
        return store.values().stream()
                .filter(c -> c.city().equalsIgnoreCase(cityName))
                .map(Customer::fullName)
                .findFirst()
                .orElse(null);
    }

    static boolean austinFallbackEnabled() {
        return Boolean.getBoolean("austin.fallback");
    }
}
