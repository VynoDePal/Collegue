// Exemple de code TypeScript pour tester le parser
import { Component, OnInit } from '@angular/core';
import * as _ from 'lodash';
const axios = require('axios');

// Interface simple
interface User {
  id: number;
  name: string;
  email?: string;
}

// Interface avec generics et héritage
interface Repository<T> extends BaseRepository {
  findById(id: string): Promise<T>;
  findAll(): Promise<T[]>;
}

// Type avec union et intersection
type UserRole = 'admin' | 'editor' | 'viewer';
type ExtendedUser = User & { role: UserRole };

// Type générique
type Result<T> = {
  data: T;
  error: Error | null;
};

// Enum
enum Status {
  Active = 'active',
  Inactive = 'inactive',
  Pending = 'pending'
}

// Classe simple
class Person {
  private name: string;
  protected age: number;
  
  constructor(name: string, age: number) {
    this.name = name;
    this.age = age;
  }
  
  public getName(): string {
    return this.name;
  }
  
  public getAge(): number {
    return this.age;
  }
}

// Classe avec héritage et implémentation d'interface
class Employee extends Person implements User {
  id: number;
  email: string;
  private department: string;
  
  constructor(id: number, name: string, age: number, email: string, department: string) {
    super(name, age);
    this.id = id;
    this.email = email;
    this.department = department;
  }
  
  public getDepartment(): string {
    return this.department;
  }
}

// Classe générique
class DataService<T> {
  private data: T[];
  
  constructor(initialData: T[] = []) {
    this.data = initialData;
  }
  
  public add(item: T): void {
    this.data.push(item);
  }
  
  public getAll(): T[] {
    return this.data;
  }
}

// Fonction standard
function calculateTax(amount: number, rate: number = 0.2): number {
  return amount * rate;
}

// Fonction avec generics
function identity<T>(arg: T): T {
  return arg;
}

// Fonction fléchée
const multiply = (a: number, b: number): number => a * b;

// Variables avec types
const user: User = { id: 1, name: 'John Doe', email: 'john@example.com' };
let status: Status = Status.Active;
const employees: Employee[] = [];

// Async/await
async function fetchData<T>(url: string): Promise<Result<T>> {
  try {
    const response = await axios.get(url);
    return { data: response.data, error: null };
  } catch (error) {
    return { data: null as any, error: error as Error };
  }
}

// Décorateur de classe (Angular style)
@Component({
  selector: 'app-user',
  template: '<div>User Component</div>'
})
class UserComponent implements OnInit {
  constructor() {}
  
  ngOnInit(): void {
    console.log('Component initialized');
  }
}

// Namespace
namespace Validation {
  export interface StringValidator {
    isValid(s: string): boolean;
  }
  
  export class EmailValidator implements StringValidator {
    isValid(s: string): boolean {
      const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
      return emailRegex.test(s);
    }
  }
}

// Export par défaut
export default UserComponent;
