// Tamam fixture — "Shoply", a realistic commerce app that exercises the flows
// real apps have: branding, search, filter, list, product detail, cart, payment,
// offers, menus. One source, two builds:
//   • correct build → every flow works        (eval expects PASS)
//   • BUGGY build    → planted defects active   (eval expects FAIL)
//
// All defects live in one place (enum BUG) so each is a single, obvious toggle.
// SwiftUI exposes Text/Button/TextField labels to the accessibility tree, which is
// what the agent perceives and the eval oracle checks. See fixtures/ios/README.md.

import SwiftUI
import UIKit
import UserNotifications

enum BUG {
    #if BUGGY
    static let brand = "Shopply"         // #1 branding: misspelled (should be Shoply)
    static let headphonesPrice = 8       // #2 list: wrong price (should be 80)
    static let offer = "5% off"          // #3 offers: wrong discount (should be 20%)
    static let searchWorks = false       // #4 search: doesn't filter
    static let filterWorks = false       // #5 filter: doesn't filter
    static let addToCartWorks = false    // #6 list/detail: Add to Cart is a no-op
    static let cartTotalCorrect = false  // #7 payment: total is wrong
    static let payWorks = false          // #8 payment: Pay is a no-op → no confirmation
    static let ordersNavWorks = false    // #9 menus: Orders row doesn't navigate
    static let hasGardenCategory = false // #10 category bar: Garden chip missing entirely
    static let collectionFilterWorks = false // #11 offers: collection chip selects but never filters
    static let filterSurvivesResume = false  // #12 lifecycle: filter resets when the app resumes
    static let reviewsLoad = false           // #13 slow load: reviews spin forever, never arrive
    static let ticketPrice = 50              // #14 infinite list: Golden Ticket priced wrong (should be 5)
    static let saveFails = true              // #15 error alert: Save shows "Couldn't save" yet the screen says Saved
    static let cancelWorks = false           // #16 confirm dialog: Cancel on "Sign out?" signs you out anyway
    static let darkModeSticks = false        // #17 switch: Dark mode snaps back off when turned on
    // #18–#21: defects only a LOOK can catch — the screen opens with the right title, so a
    // check that the page appeared passes; what is wrong is what the page shows.
    static let termsHasBody = false          // #18 blank page: Terms of Service opens empty
    static let helpLoadsAll = false          // #19 half-loaded: 2 of 6 topics, then spins forever
    static let shippingOpensShipping = false // #20 wrong page: the Shipping row opens Returns
    static let rewardsLoadClean = false      // #21 error banner: balance shows, but under an error
    static let ratingCorrect = false         // #22 coordinate-only: the 4th star records 3 stars
    static let addressKeepsPostcode = false  // #23 keyboard-covered form: the postcode is dropped
    static let holdRedeems = false           // #24 long-press: holding "Hold to redeem" does nothing
    static let couponSwipes = false          // #25 swipe card: the coupon snaps back, code never shown
    static let feedbackHonorsAgreement = false // #26 feedback: Send ignores the agreement box
    static let newsletterKeepsEmail = false  // #27 merged form: Subscribe drops the typed email
    #else
    static let brand = "Shoply"
    static let headphonesPrice = 80
    static let offer = "20% off"
    static let searchWorks = true
    static let filterWorks = true
    static let addToCartWorks = true
    static let cartTotalCorrect = true
    static let payWorks = true
    static let ordersNavWorks = true
    static let hasGardenCategory = true
    static let collectionFilterWorks = true
    static let filterSurvivesResume = true
    static let reviewsLoad = true
    static let ticketPrice = 5
    static let saveFails = false
    static let cancelWorks = true
    static let darkModeSticks = true
    static let termsHasBody = true
    static let helpLoadsAll = true
    static let shippingOpensShipping = true
    static let rewardsLoadClean = true
    static let ratingCorrect = true
    static let addressKeepsPostcode = true
    static let holdRedeems = true
    static let couponSwipes = true
    static let feedbackHonorsAgreement = true
    static let newsletterKeepsEmail = true
    #endif
}

struct Product: Identifiable, Hashable {
    let id = UUID()
    let name: String
    let price: Int
    let category: String
}

struct Category { let name: String; let icon: String }

final class Store: ObservableObject {
    /// Icon-only category chips. More than fit the screen, so the tail is reachable
    /// only by scrolling the row sideways.
    static var categories: [Category] {
        var all = [
            Category(name: "All", icon: "square.grid.2x2"),
            Category(name: "Electronics", icon: "bolt"),
            Category(name: "Books", icon: "book"),
            Category(name: "Toys", icon: "teddybear"),
            Category(name: "Sports", icon: "figure.run"),
            Category(name: "Music", icon: "guitars"),
        ]
        if BUG.hasGardenCategory { all.append(Category(name: "Garden", icon: "leaf")) }
        return all
    }

    @Published var search = ""
    @Published var filter = "All"
    @Published var cart: [Product] = []

    let products: [Product] = [
        Product(name: "Wireless Mouse", price: 25, category: "Electronics"),
        Product(name: "USB Cable", price: 10, category: "Electronics"),
        Product(name: "Headphones", price: BUG.headphonesPrice, category: "Electronics"),
        Product(name: "Swift Book", price: 40, category: "Books"),
        Product(name: "Design Book", price: 35, category: "Books"),
        Product(name: "Watering Can", price: 15, category: "Garden"),
        Product(name: "Plant Pot", price: 12, category: "Garden"),
    ]

    var visible: [Product] {
        products.filter { p in
            let s = !BUG.searchWorks || search.isEmpty || p.name.lowercased().contains(search.lowercased())
            let f = !BUG.filterWorks || filter == "All" || p.category == filter
            return s && f
        }
    }

    var total: Int {
        let sum = cart.reduce(0) { $0 + $1.price }
        return BUG.cartTotalCorrect ? sum : max(0, sum - 5)
    }
}

@main
struct ShoplyApp: App {
    @StateObject private var store = Store()
    var body: some Scene {
        WindowGroup { RootView().environmentObject(store) }
    }
}

struct RootView: View {
    var body: some View {
        TabView {
            NavigationStack { ShopView() }.tabItem { Text("Shop") }
            NavigationStack { OffersView() }.tabItem { Text("Offers") }
            NavigationStack { CartView() }.tabItem { Text("Cart") }
            NavigationStack { MoreView() }.tabItem { Text("More") }
        }
    }
}

struct ShopView: View {
    @EnvironmentObject var store: Store
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        VStack(spacing: 12) {
            Text(BUG.brand).font(.largeTitle).bold()
            TextField("Search", text: $store.search)
                .textFieldStyle(.roundedBorder).padding(.horizontal)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(Store.categories, id: \.name) { c in
                        Button {
                            store.filter = c.name
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: c.icon)
                                Text(c.name)
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                }
                .padding(.horizontal)
            }
            List(store.visible) { p in
                NavigationLink(value: p) {
                    HStack { Text(p.name); Spacer(); Text("$\(p.price)") }
                }
            }
            .navigationDestination(for: Product.self) { ProductView(product: $0) }
        }
        .navigationTitle("Shop")
        .onChange(of: scenePhase) { phase in
            // State that is only held in memory is lost when the app is resumed — the
            // classic bug that no amount of tapping inside the app can reveal.
            if phase == .active && !BUG.filterSurvivesResume { store.filter = "All" }
        }
    }
}

struct ProductView: View {
    @EnvironmentObject var store: Store
    let product: Product
    @State private var added = false
    var body: some View {
        VStack(spacing: 16) {
            Text(product.name).font(.title)
            Text("Price: $\(product.price)")
            Button("Add to Cart") {
                if BUG.addToCartWorks { store.cart.append(product); added = true }
            }.buttonStyle(.borderedProminent)
            if added { Text("Added to cart") }
        }
        .navigationTitle(product.name)
    }
}

struct CartView: View {
    @EnvironmentObject var store: Store
    var body: some View {
        VStack(spacing: 12) {
            if store.cart.isEmpty {
                Text("Cart is empty")
            } else {
                List(store.cart) { p in
                    HStack { Text(p.name); Spacer(); Text("$\(p.price)") }
                }
                Text("Total: $\(store.total)").font(.headline)
                NavigationLink("Checkout") { PaymentView() }.buttonStyle(.borderedProminent)
            }
        }
        .navigationTitle("Cart")
    }
}

struct PaymentView: View {
    @EnvironmentObject var store: Store
    @State private var confirmed = false
    var body: some View {
        VStack(spacing: 16) {
            if confirmed {
                Text("Order Confirmed").font(.title)
            } else {
                Text("Amount due: $\(store.total)")
                Button("Pay") { if BUG.payWorks { confirmed = true } }
                    .buttonStyle(.borderedProminent)
            }
        }
        .navigationTitle("Payment")
    }
}

struct OffersView: View {
    @EnvironmentObject var store: Store
    /// Icon-only collection chips: the chip shows its NAME only while selected, so what
    /// each one is can only be found out by picking it. A widespread pattern (filter
    /// strips, tab bars, toolbars) and the reason an asset name in the tree says nothing.
    @State private var collection = ""
    private let chips = [
        Category(name: "Electronics", icon: "bolt"),
        Category(name: "Books", icon: "book"),
        Category(name: "Garden", icon: "leaf"),
    ]

    var body: some View {
        VStack(spacing: 16) {
            Text("\(BUG.offer) everything").font(.title2).bold()
            Text("Limited time offer")
            HStack(spacing: 10) {
                ForEach(chips, id: \.name) { c in
                    Button {
                        collection = c.name
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: c.icon)
                            if collection == c.name { Text(c.name) }
                        }
                    }
                    .buttonStyle(.bordered)
                }
            }
            List(store.products.filter {
                collection.isEmpty || !BUG.collectionFilterWorks || $0.category == collection
            }) { p in
                HStack { Text(p.name); Spacer(); Text("$\(p.price)") }
            }
        }
        .navigationTitle("Offers")
    }
}

struct MoreView: View {
    var body: some View {
        List {
            NavigationLink("Profile") { InfoScreen(title: "Profile", text: "test@example.com") }
            if BUG.ordersNavWorks {
                NavigationLink("Orders") { InfoScreen(title: "Orders", text: "No orders yet") }
            } else {
                Text("Orders")   // dead: renders but does not navigate
            }
            NavigationLink("About") { InfoScreen(title: "About", text: "\(BUG.brand) v1.0") }
            NavigationLink("Help") { InfoScreen(title: "Help", text: "Contact support") }
            NavigationLink("Reviews") { ReviewsView() }
            NavigationLink("Catalog") { CatalogScreen() }
            NavigationLink("Settings") { SettingsView() }
            // One row for the look-only pages (#18–#21): more rows here would change the
            // screen every older More scenario was measured on.
            NavigationLink("Help & Legal") { HelpLegalView() }
        }
        .navigationTitle("More")
    }
}

struct HelpLegalView: View {
    var body: some View {
        List {
            NavigationLink("Terms of Service") { TermsView() }
            NavigationLink("Help Topics") { HelpTopicsView() }
            NavigationLink("Shipping") {
                if BUG.shippingOpensShipping {
                    InfoScreen(title: "Shipping", text: "Orders ship within 2 business days. Free shipping over $50.")
                } else {
                    InfoScreen(title: "Returns", text: "Return any item within 30 days for a full refund.")
                }
            }
            NavigationLink("Rewards") { RewardsView() }
            NavigationLink("Rate Us") { RateView() }
            NavigationLink("Delivery Address") { AddressView() }
            NavigationLink("Gift Card") { GiftCardView() }
            NavigationLink("Coupon") { CouponView() }
            NavigationLink("Feedback") { FeedbackView() }
            NavigationLink("Newsletter") { NewsletterView() }
        }
        .navigationTitle("Help & Legal")
    }
}

struct ReviewsView: View {
    @State private var loaded = false
    var body: some View {
        Group {
            if loaded && BUG.reviewsLoad {
                List {
                    Text("Great shop, fast delivery")
                    Text("Exactly as described")
                }
            } else {
                ProgressView("Loading reviews")
            }
        }
        .navigationTitle("Reviews")
        .task {
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            loaded = true
        }
    }
}

/// #14 — an INFINITE list, built the way many real apps build one: a UIKit table whose
/// data source loads the next page when the LAST loaded row is requested. A full
/// accessibility walk requests every row, so a harness that walks the whole tree pages in
/// data the user never scrolled to (a bug seen on a real app's feed). The
/// "Loaded: N items" header makes that visible: after an honest scroll to the ticket it
/// reads ~80; a tree-walking harness drives it far higher. The Golden Ticket sits at row
/// 70 — deep enough to need ~10 scrolls (repeated legitimate swipes) — and its price is
/// the planted defect.
struct CatalogScreen: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> CatalogController { CatalogController() }
    func updateUIViewController(_ vc: CatalogController, context: Context) {}
}

final class CatalogController: UITableViewController {
    static let pageSize = 20
    static let ticketRow = 70
    private var loaded = CatalogController.pageSize
    private let header = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "Catalog"
        tableView.register(UITableViewCell.self, forCellReuseIdentifier: "c")
        header.textAlignment = .center
        header.frame = CGRect(x: 0, y: 0, width: 320, height: 44)
        tableView.tableHeaderView = header
        refreshHeader()
    }

    private func refreshHeader() { header.text = "Loaded: \(loaded) items" }

    private func name(_ row: Int) -> String {
        row == CatalogController.ticketRow
            ? "Golden Ticket — $\(BUG.ticketPrice)"
            : "Catalog item \(row + 1) — $\(row % 9 + 1)"
    }

    override func tableView(_ tv: UITableView, numberOfRowsInSection section: Int) -> Int { loaded }

    override func tableView(_ tv: UITableView, cellForRowAt ip: IndexPath) -> UITableViewCell {
        let cell = tv.dequeueReusableCell(withIdentifier: "c", for: ip)
        cell.textLabel?.text = name(ip.row)
        if ip.row == loaded - 1 {            // asked for the last row → load the next page
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.loaded += CatalogController.pageSize
                self.refreshHeader()
                self.tableView.reloadData()
            }
        }
        return cell
    }
}

/// Dialogs from two sources. "Enable order updates" asks iOS for notification permission —
/// a SYSTEM dialog the harness must get past (why autoAcceptAlerts existed). The app's own
/// dialogs — the "Couldn't save" error (defect #15) and the "Sign out?" confirmation
/// (defect #16) — are the agent's to see and answer; accepting them blindly is a bug.
struct SettingsView: View {
    @State private var orderUpdates = false
    @State private var name = "Sam"
    @State private var status = ""
    @State private var saveError = false
    @State private var signedIn = true
    @State private var confirmSignOut = false
    @State private var darkMode = false
    var body: some View {
        Form {
            // A standard settings switch: a labelled row wrapping the knob. Only the knob
            // flips it, and its state lives in the accessibility value (defect #17).
            Toggle("Dark mode", isOn: Binding(get: { darkMode },
                                              set: { on in if BUG.darkModeSticks { darkMode = on } }))
            Text("Theme: \(darkMode ? "Dark" : "Light")")  // plain text: the eval oracle's independent read
            Text("Order updates: \(orderUpdates ? "On" : "Off")")
            Button("Enable order updates") {
                UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
                    DispatchQueue.main.async { orderUpdates = granted }
                }
            }
            TextField("Display name", text: $name)
            Button("Save") {
                if BUG.saveFails { saveError = true }
                status = "Saved"
            }
            if !status.isEmpty { Text(status) }
            Text(signedIn ? "Signed in as Sam" : "Signed out")
            if signedIn { Button("Sign out", role: .destructive) { confirmSignOut = true } }
        }
        .navigationTitle("Settings")
        .alert("Couldn't save your changes", isPresented: $saveError) {
            Button("OK", role: .cancel) {}
        } message: { Text("Something went wrong. Please try again.") }
        .alert("Sign out?", isPresented: $confirmSignOut) {
            Button("Cancel", role: .cancel) { if !BUG.cancelWorks { signedIn = false } }
            Button("Sign out", role: .destructive) { signedIn = false }
        }
    }
}

struct InfoScreen: View {
    let title: String
    let text: String
    var body: some View {
        Text(text).navigationTitle(title)
    }
}

/// #18 — the page opens with its title, but the body never renders.
struct TermsView: View {
    var body: some View {
        ScrollView {
            if BUG.termsHasBody {
                VStack(alignment: .leading, spacing: 12) {
                    Text("1. Using Shoply").font(.headline)
                    Text("By creating an account you agree to these terms.")
                    Text("2. Orders and payment").font(.headline)
                    Text("Prices include tax. You can cancel an order before it ships.")
                    Text("Last updated: 1 September 2026").font(.footnote)
                }
                .padding()
            }
        }
        .navigationTitle("Terms of Service")
    }
}

/// #19 — the list renders its first rows, then the rest never arrive.
struct HelpTopicsView: View {
    static let topics = ["Orders", "Shipping", "Returns", "Payments", "Account", "Gift cards"]
    @State private var shown = 2
    var body: some View {
        List {
            ForEach(Self.topics.prefix(shown), id: \.self) { Text("Help: \($0)") }
            if shown < Self.topics.count {
                ProgressView("Loading more topics")
            } else {
                Text("All \(Self.topics.count) topics").font(.footnote)
            }
        }
        .navigationTitle("Help Topics")
        .task {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if BUG.helpLoadsAll { shown = Self.topics.count }
        }
    }
}

/// #21 — the balance shows, but the screen also reports that loading failed.
struct RewardsView: View {
    var body: some View {
        VStack(spacing: 16) {
            if !BUG.rewardsLoadClean {
                Text("Couldn't load your latest rewards. Pull to retry.")
                    .foregroundColor(.white).padding(8).background(Color.red)
            }
            Text("Balance: 120 points").font(.title2)
            Text("Earn 1 point for every $1 you spend.")
            Spacer()
        }
        .padding()
        .navigationTitle("Rewards")
    }
}

/// #22 — stars drawn with no accessibility (hidden from the tree, like a custom-drawn rating
/// widget): the only way to tap one is by its position on the screen.
struct RateView: View {
    @State private var rating = 0
    var body: some View {
        VStack(spacing: 28) {
            Text("How would you rate Shoply?")
            HStack(spacing: 18) {
                ForEach(1...5, id: \.self) { i in
                    Image(systemName: i <= rating ? "star.fill" : "star")
                        .resizable()
                        .frame(width: 44, height: 44)
                        .foregroundColor(.orange)
                        .contentShape(Rectangle())
                        .onTapGesture { rating = BUG.ratingCorrect ? i : max(1, i - 1) }
                }
            }
            .accessibilityHidden(true)
            if rating > 0 { Text("Thanks for \(rating) stars") }
            Spacer()
        }
        .padding(.top, 40)
        .navigationTitle("Rate Us")
    }
}

/// #23 — a form low on the screen: once the first field is focused, the keyboard covers the
/// next fields and the Save button (the harness must hide it, not tap through it).
struct AddressView: View {
    enum Field { case street, city, postcode }
    @State private var street = ""
    @State private var city = ""
    @State private var postcode = ""
    @State private var status = ""
    @FocusState private var focus: Field?
    var body: some View {
        // A scrolling form, like real ones: the keyboard COVERS the lower fields and the Save
        // button (they stay in the tree, under it) instead of squeezing them off the screen.
        ScrollView {
        VStack(alignment: .leading, spacing: 14) {
            Color.clear.frame(height: 320)
            Text("Where should we deliver?")
            TextField("Street", text: $street).textFieldStyle(.roundedBorder).focused($focus, equals: .street)
            TextField("City", text: $city).textFieldStyle(.roundedBorder).focused($focus, equals: .city)
            TextField("Postcode", text: $postcode).textFieldStyle(.roundedBorder).focused($focus, equals: .postcode)
            Button("Save address") {
                let pc = BUG.addressKeepsPostcode ? postcode : ""
                status = street.isEmpty || city.isEmpty || pc.isEmpty ? "Postcode is required" : "Address saved"
                if street.isEmpty || city.isEmpty { status = "Street and city are required" }
            }
            if !status.isEmpty { Text(status) }
        }
        .padding()
        }
        .scrollDismissesKeyboard(.never)
        .navigationTitle("Delivery Address")
        .onAppear { focus = .street }
    }
}

/// #24 — an action that needs a press-and-hold, not a tap (a tap does nothing by design).
struct GiftCardView: View {
    @State private var redeemed = false
    var body: some View {
        VStack(spacing: 24) {
            Text("Gift card: SHOPLY-2026")
            Text("Hold to redeem")
                .padding()
                .background(Color.orange.opacity(0.2))
                .accessibilityAddTraits(.isButton)
                .onLongPressGesture(minimumDuration: 0.8) { if BUG.holdRedeems { redeemed = true } }
            if redeemed { Text("Gift card redeemed") }
            Spacer()
        }
        .padding(.top, 40)
        .navigationTitle("Gift Card")
    }
}

/// #25 — a card that must be SWIPED aside (a flick), not tapped, to reveal what is under it.
struct CouponView: View {
    @State private var offset: CGFloat = 0
    @State private var revealed = false
    var body: some View {
        VStack(spacing: 24) {
            Text("Swipe the coupon left to reveal your code")
            ZStack {
                if revealed { Text("Code: SAVE20") }
                Text("Your coupon")
                    .frame(width: 280, height: 120)
                    .background(Color.orange.opacity(0.3))
                    .cornerRadius(12)
                    .offset(x: offset)
                    .gesture(DragGesture()
                        .onChanged { offset = min(0, $0.translation.width) }
                        .onEnded { v in
                            if BUG.couponSwipes && v.translation.width < -80 {
                                offset = -400; revealed = true
                            } else { offset = 0 }
                        })
                    .accessibilityLabel("Your coupon")
            }
            Spacer()
        }
        .padding(.top, 40)
        .navigationTitle("Coupon")
    }
}

/// #26 — two patterns found in real apps (React Native, Flutter): a MULTI-LINE comment box (a
/// TextView in the tree, which the agent once never saw), and an agreement row whose checkbox
/// and label are ONE accessibility element where only the checkbox square toggles it — a tap
/// on the row's centre lands on the label and does nothing. Its keyboard has no Done key.
struct FeedbackView: View {
    @State private var comments = ""
    @State private var agreed = false
    @State private var status = ""
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("Tell us what you think")
                TextEditor(text: $comments)
                    .frame(height: 120)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.gray.opacity(0.4)))
                    .accessibilityLabel("Comments")
                HStack(spacing: 12) {
                    Image(systemName: agreed ? "checkmark.square.fill" : "square")
                        .font(.title2)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                        .onTapGesture { agreed.toggle() }
                    Text("I agree to be contacted about my feedback")
                    Spacer()
                }
                .frame(maxWidth: .infinity, minHeight: 56)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("I agree to be contacted about my feedback")
                .accessibilityValue(agreed ? "1" : "0")
                .modifier(ToggleTrait())
                Button("Send feedback") {
                    let ok = BUG.feedbackHonorsAgreement ? agreed : false
                    if comments.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        status = "Please write a comment"
                    } else if !ok {
                        status = "Please agree to be contacted"
                    } else {
                        status = "Thanks for your feedback"
                    }
                }
                .buttonStyle(.borderedProminent)
                if !status.isEmpty { Text(status) }
            }
            .padding()
        }
        .navigationTitle("Feedback")
    }
}

/// The two-state trait, so the tree reports the row's on/off value (iOS 17+).
struct ToggleTrait: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 17.0, *) {
            content.accessibilityAddTraits(.isToggle)
        } else {
            content.accessibilityAddTraits(.isButton)
        }
    }
}

/// #27 — a FLATTENED form (React Native's accessible={true} on a container): the label, the
/// email field and the Subscribe button are ONE accessibility element, so the tree shows no field
/// to type into and no button to tap. Only the pixels show them.
struct NewsletterView: View {
    @State private var email = ""
    @State private var status = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Join our newsletter").font(.headline)
            Text("Email")
            TextField("", text: $email)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(10)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.gray.opacity(0.6), lineWidth: 1))
            Button("Subscribe") {
                let kept = BUG.newsletterKeepsEmail ? email : ""
                status = "Subscribed: \(kept)"
            }
            .buttonStyle(.borderedProminent)
        }
        .padding()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Join our newsletter, Email, Subscribe")
        .overlay(alignment: .bottom) {
            if !status.isEmpty { Text(status).offset(y: 60) }
        }
        Spacer()
        .navigationTitle("Newsletter")
    }
}
