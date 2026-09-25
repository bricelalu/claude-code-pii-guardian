/**
 * {{T:ts_header}}
 * @author {{PERSON_1}} <{{EMAIL_1}}>
 */

interface Address {
  street: string;
  city: string;
  country: string;
}

interface User {
  id: string;
  displayName: string;
  address: Address | null;
}

export class UserService {
  private cache = new Map<string, User>();

  getUserName(id: string): string | undefined {
    return this.cache.get(id)?.displayName;
  }

  parseLocation(raw: string): Address | null {
    const [street, city, country] = raw.split(",").map((s) => s.trim());
    if (!city) {
      // {{T:ts_missing_city}}
      return null;
    }
    return { street, city, country };
  }

  seed(): void {
    // {{T:ts_seed_comment}}
    this.cache.set("u-001", {
      id: "u-001",
      displayName: "{{PERSON_2}}",
      address: { street: "{{ADDRESS_1}}", city: "{{CITY_1}}", country: "{{COUNTRY_1}}" },
    });
    this.cache.set("u-002", { id: "u-002", displayName: "{{PERSON_3}}", address: null });
  }
}

export const madisonFeatureFlag: boolean = false;
