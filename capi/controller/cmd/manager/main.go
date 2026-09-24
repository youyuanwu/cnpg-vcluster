package main

import (
	"errors"
	"flag"
	"os"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	runtimewebhook "sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	tenantcontroller "github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/controller"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/sanitize"
	tenantwebhook "github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/webhook"
)

func main() {
	var (
		leaderElect      bool
		mutationEnabled  bool
		metricsAddress   string
		probeAddress     string
		webhookCertDir   string
		supportedVersion string
		controllerImage  string
	)
	flag.BoolVar(&leaderElect, "leader-elect", true, "Enable leader election")
	flag.BoolVar(&mutationEnabled, "mutation-enabled", false, "Enable Tenant provider mutation")
	flag.StringVar(&metricsAddress, "metrics-bind-address", "0", "Metrics bind address")
	flag.StringVar(&probeAddress, "health-probe-bind-address", ":8081", "Health probe bind address")
	flag.StringVar(&webhookCertDir, "webhook-cert-dir", "/var/run/tenant-controller/tls", "Webhook certificate directory")
	flag.StringVar(&supportedVersion, "supported-kubernetes-version", "1.36.4", "Supported Tenant Kubernetes version")
	flag.StringVar(&controllerImage, "controller-image", "", "Exact Tenant controller image identity")
	options := zap.Options{Development: false}
	options.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(sanitize.Logger(zap.New(zap.UseFlagOptions(&options))))

	scheme := runtime.NewScheme()
	must(clientgoscheme.AddToScheme(scheme))
	must(tenancyv1alpha1.AddToScheme(scheme))
	manager, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		LeaderElection:         leaderElect,
		LeaderElectionID:       "tenant-controller.tenancy.cnpg-vcluster.io",
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		WebhookServer: runtimewebhook.NewServer(runtimewebhook.Options{
			Port:    9443,
			CertDir: webhookCertDir,
		}),
	})
	must(err)
	must((&tenantcontroller.TenantReconciler{
		Client:                  manager.GetClient(),
		APIReader:               manager.GetAPIReader(),
		Docker:                  tenantcontroller.NewDockerClient("/var/run/docker.sock"),
		SupportedVersion:        supportedVersion,
		MutationEnabled:         mutationEnabled,
		ExpectedControllerImage: controllerImage,
	}).SetupWithManager(manager))
	manager.GetWebhookServer().Register(
		"/validate-tenancy-cnpg-vcluster-io-v1alpha1-tenant",
		&admission.Webhook{Handler: &tenantwebhook.TenantValidator{
			SupportedVersion: supportedVersion,
		}},
	)
	must(manager.AddHealthzCheck("healthz", healthz.Ping))
	must(manager.AddReadyzCheck("readyz", healthz.Ping))
	must(manager.Start(ctrl.SetupSignalHandler()))
}

func must(err error) {
	if err != nil {
		ctrl.Log.Error(errors.New(sanitize.Text(err.Error())), "fatal controller error")
		os.Exit(1)
	}
}
