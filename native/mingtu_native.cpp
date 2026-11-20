#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <QAction>
#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QCoreApplication>
#include <QDialog>
#include <QFileDialog>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QInputDialog>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QSignalBlocker>
#include <QScrollBar>
#include <QTableWidget>
#include <QIcon>
#include <QLabel>
#include <QMainWindow>
#include <QMenu>
#include <QMessageBox>
#include <QMetaObject>
#include <QPushButton>
#include <QStyle>
#include <QSystemTrayIcon>
#include <QTextEdit>
#include <QThread>
#include <QTimer>
#include <QVBoxLayout>
#include <QWidget>

#include <algorithm>
#include <memory>

namespace {

class Dashboard;

typedef struct {
    PyObject_HEAD
    Dashboard *dashboard;
} BridgeObject;

PyTypeObject BridgeType = {
    PyVarObject_HEAD_INIT(nullptr, 0)
};

bool bridge_is_live(BridgeObject *self) {
    if (self->dashboard != nullptr) {
        return true;
    }
    PyErr_SetString(PyExc_RuntimeError, "Qt UI bridge is no longer active");
    return false;
}

QString py_unicode_to_qstring(PyObject *value) {
    const char *utf8 = PyUnicode_AsUTF8(value);
    if (utf8 == nullptr) {
        return {};
    }
    return QString::fromUtf8(utf8);
}

class Dashboard final : public QObject {
public:
    Dashboard(PyObject *controller, const QString &application_name, bool show_windows)
        : controller_(Py_NewRef(controller)) {
        QApplication::setApplicationName(application_name);
        QApplication::setOrganizationName(QStringLiteral("Mingtu AI"));
        QApplication::setQuitOnLastWindowClosed(false);
        build_main_window();
        build_history_window();
        build_settings_window();
        build_tray();

        QObject::connect(qApp, &QCoreApplication::aboutToQuit, [this]() {
            stop_controller();
        });
        if (show_windows || !QSystemTrayIcon::isSystemTrayAvailable()) {
            main_window_->show();
        }
    }

    ~Dashboard() {
        if (tray_icon_ != nullptr) {
            tray_icon_->hide();
        }
        delete history_window_;
        delete settings_window_;
        delete main_window_;
        Py_DECREF(controller_);
    }

    void show_home() {
        main_window_->show();
        main_window_->raise();
        main_window_->activateWindow();
    }

    void show_history() {
        history_window_->show();
        history_window_->raise();
        history_window_->activateWindow();
    }

    void show_settings() {
        settings_window_->show();
        settings_window_->raise();
        settings_window_->activateWindow();
    }

    void set_pending_count(int count) {
        const int bounded = std::max(0, count);
        pending_label_->setText(QStringLiteral("待處理檔案：%1").arg(bounded));
        pending_action_->setText(QStringLiteral("待處理檔案：%1").arg(bounded));
    }

    void set_model_status(const QString &status) {
        model_label_->setText(QStringLiteral("模型狀態：") + status);
    }

    void set_paused(bool paused) {
        paused_ = paused;
        monitor_label_->setText(
            paused ? QStringLiteral("監控狀態：已暫停")
                   : QStringLiteral("監控狀態：監控中"));
        pause_action_->setText(
            paused ? QStringLiteral("恢復監控") : QStringLiteral("暫停監控"));
    }

    void set_proposals_json(const QString &text) {
        if (text == proposals_json_) return;
        proposals_json_ = text;
        const QJsonArray rows = QJsonDocument::fromJson(text.toUtf8()).array();
        populate(proposals_, rows, false);
        proposal_scope_->setText(QStringLiteral("待決建議：%1 筆（本次啟動；結束程式後不保留操作入口）").arg(rows.size()));
        update_action_buttons();
    }

    void set_history_json(const QString &text) {
        if (text == history_json_) return;
        history_json_ = text;
        populate(history_, QJsonDocument::fromJson(text.toUtf8()).array(), true);
        update_action_buttons();
    }

    void set_action_busy(bool busy) {
        action_busy_ = busy;
        update_action_buttons();
    }

    void set_action_message(const QString &message) {
        action_message_->setText(message);
        history_message_->setText(message);
    }

    // Private smoke hooks exercise the same selection and button callbacks as
    // the visible UI. They neither accept arbitrary paths nor execute code.
    void test_action(const QString &action, qint64 id, const QString &stem) {
        QTableWidget *table = action == QStringLiteral("undo") ? history_ : proposals_;
        for (int row = 0; row < table->rowCount(); ++row) {
            if (table->item(row, 0)->data(Qt::UserRole).toLongLong() != id) continue;
            table->selectRow(row);
            if (action == QStringLiteral("edit") || action == QStringLiteral("cancel_edit")) {
                QTimer::singleShot(0, this, [stem, action]() {
                    for (QWidget *widget : QApplication::topLevelWidgets()) {
                        if (auto *dialog = qobject_cast<QInputDialog *>(widget)) {
                            dialog->setTextValue(stem);
                            if (action == QStringLiteral("cancel_edit")) dialog->reject();
                            else dialog->accept();
                        }
                    }
                });
                edit_button_->click();
            } else if (action == QStringLiteral("accept")) accept_button_->click();
            else if (action == QStringLiteral("ignore")) ignore_button_->click();
            else if (action == QStringLiteral("undo")) undo_button_->click();
            return;
        }
        set_action_message(QStringLiteral("Smoke: 找不到指定事件列"));
    }

    void save_screenshot(const QString &path) {
        main_window_->grab().save(path);
    }

    void set_settings_text(const QString &text) {
        settings_text_->setPlainText(text);
    }

    void set_watch_directories_json(const QString &text) {
        if (watch_list_ == nullptr) return;
        if (text == watch_directories_json_) return;
        watch_directories_json_ = text;
        watch_list_->clear();
        const QJsonArray rows = QJsonDocument::fromJson(text.toUtf8()).array();
        for (const QJsonValue &val : rows) {
            watch_list_->addItem(val.toString());
        }
    }

    void add_watch_directory_action() {
        const QString dir = QFileDialog::getExistingDirectory(
            settings_window_,
            QStringLiteral("選擇要監控的資料夾"),
            QString(),
            QFileDialog::ShowDirsOnly | QFileDialog::DontResolveSymlinks
        );
        if (dir.trimmed().isEmpty()) return;
        PyGILState_STATE gil = PyGILState_Ensure();
        PyObject *dir_str = PyUnicode_FromString(dir.toUtf8().constData());
        if (dir_str != nullptr) {
            PyObject *result = PyObject_CallMethod(controller_, "add_watch_directory", "O", dir_str);
            Py_DECREF(dir_str);
            if (result == nullptr) {
                PyErr_Print();
                set_action_message(QStringLiteral("無法新增監控資料夾，請查看終端記錄。"));
            } else {
                Py_DECREF(result);
            }
        }
        PyGILState_Release(gil);
    }

    void remove_watch_directory_action() {
        if (watch_list_ == nullptr) return;
        QListWidgetItem *item = watch_list_->currentItem();
        if (item == nullptr) return;
        const QString dir = item->text();
        PyGILState_STATE gil = PyGILState_Ensure();
        PyObject *dir_str = PyUnicode_FromString(dir.toUtf8().constData());
        if (dir_str != nullptr) {
            PyObject *result = PyObject_CallMethod(controller_, "remove_watch_directory", "O", dir_str);
            Py_DECREF(dir_str);
            if (result == nullptr) {
                PyErr_Print();
                set_action_message(QStringLiteral("無法移除監控資料夾，請查看終端記錄。"));
            } else {
                Py_DECREF(result);
            }
        }
        PyGILState_Release(gil);
    }

    void batch_rename_folder_action() {
        QDialog dialog(main_window_);
        dialog.setWindowTitle(QStringLiteral("命圖 AI｜一鍵資料夾批次重命名"));
        dialog.resize(540, 280);

        auto *layout = new QVBoxLayout(&dialog);

        auto *intro = new QLabel(QStringLiteral("選擇資料夾並進行一鍵 AI 批次重新命名與照片整理："), &dialog);
        intro->setStyleSheet(QStringLiteral("font-weight: bold;"));
        layout->addWidget(intro);

        // Directory selection
        auto *dir_layout = new QHBoxLayout();
        auto *dir_edit = new QLineEdit(&dialog);
        dir_edit->setPlaceholderText(QStringLiteral("請點擊右側按鈕選擇資料夾…"));
        auto *browse_btn = new QPushButton(QStringLiteral("選擇資料夾…"), &dialog);
        dir_layout->addWidget(dir_edit, 1);
        dir_layout->addWidget(browse_btn);
        layout->addLayout(dir_layout);

        QObject::connect(browse_btn, &QPushButton::clicked, [&dialog, dir_edit]() {
            const QString dir = QFileDialog::getExistingDirectory(
                &dialog,
                QStringLiteral("選擇要重命名的資料夾"),
                QString(),
                QFileDialog::ShowDirsOnly | QFileDialog::DontResolveSymlinks
            );
            if (!dir.isEmpty()) {
                dir_edit->setText(dir);
            }
        });

        // Preset Combo Box
        auto *preset_layout = new QHBoxLayout();
        auto *preset_label = new QLabel(QStringLiteral("命名策略範本："), &dialog);
        auto *preset_combo = new QComboBox(&dialog);
        preset_combo->addItem(QStringLiteral("預設 AI 語意摘要 (Default)"), QStringLiteral("default"));
        preset_combo->addItem(QStringLiteral("照片日期時間 + AI 摘要 (Photo Date Prefix)"), QStringLiteral("photo_date"));
        preset_combo->addItem(QStringLiteral("檔案類別前綴 + AI 摘要 (Category Prefix)"), QStringLiteral("category_ai"));
        preset_layout->addWidget(preset_label);
        preset_layout->addWidget(preset_combo, 1);
        layout->addLayout(preset_layout);

        // AI Hint
        auto *hint_layout = new QHBoxLayout();
        auto *hint_label = new QLabel(QStringLiteral("AI 命名提示詞："), &dialog);
        auto *hint_edit = new QLineEdit(&dialog);
        hint_edit->setPlaceholderText(QStringLiteral("選填，例如：2026沖繩旅遊照片、財務報表"));
        hint_layout->addWidget(hint_label);
        hint_layout->addWidget(hint_edit, 1);
        layout->addLayout(hint_layout);

        // Recursive Checkbox
        auto *recursive_check = new QCheckBox(QStringLiteral("遞迴包含子資料夾內的所有檔案"), &dialog);
        layout->addWidget(recursive_check);

        // Buttons
        auto *btn_layout = new QHBoxLayout();
        auto *run_btn = new QPushButton(QStringLiteral("🚀 一鍵分析並改名"), &dialog);
        run_btn->setStyleSheet(QStringLiteral("font-weight: bold; background-color: #27ae60; color: white; padding: 6px 12px;"));
        auto *preview_btn = new QPushButton(QStringLiteral("🔍 僅產生建議預覽"), &dialog);
        auto *cancel_btn = new QPushButton(QStringLiteral("取消"), &dialog);

        btn_layout->addWidget(run_btn);
        btn_layout->addWidget(preview_btn);
        btn_layout->addWidget(cancel_btn);
        layout->addLayout(btn_layout);

        QObject::connect(cancel_btn, &QPushButton::clicked, [&dialog]() { dialog.reject(); });

        bool run_clicked = false;
        bool auto_accept = false;

        QObject::connect(run_btn, &QPushButton::clicked, [&dialog, &run_clicked, &auto_accept]() {
            run_clicked = true;
            auto_accept = true;
            dialog.accept();
        });

        QObject::connect(preview_btn, &QPushButton::clicked, [&dialog, &run_clicked, &auto_accept]() {
            run_clicked = true;
            auto_accept = false;
            dialog.accept();
        });

        if (dialog.exec() != QDialog::Accepted || !run_clicked) {
            return;
        }

        const QString target_dir = dir_edit->text().trimmed();
        if (target_dir.isEmpty()) {
            QMessageBox::warning(main_window_, QStringLiteral("命圖 AI"), QStringLiteral("請選擇有效的資料夾！"));
            return;
        }

        const QString preset = preset_combo->currentData().toString();
        const QString hint = hint_edit->text().trimmed();
        const bool recursive = recursive_check->isChecked();

        PyGILState_STATE gil = PyGILState_Ensure();
        PyObject *dir_obj = PyUnicode_FromString(target_dir.toUtf8().constData());
        PyObject *preset_obj = PyUnicode_FromString(preset.toUtf8().constData());
        PyObject *hint_obj = hint.isEmpty() ? Py_NewRef(Py_None) : PyUnicode_FromString(hint.toUtf8().constData());

        if (dir_obj != nullptr && preset_obj != nullptr && hint_obj != nullptr) {
            PyObject *result = PyObject_CallMethod(
                controller_,
                "request_batch_analyze",
                "OpOOO",
                dir_obj,
                recursive ? 1 : 0,
                preset_obj,
                hint_obj,
                auto_accept ? Py_True : Py_False
            );
            Py_XDECREF(dir_obj);
            Py_XDECREF(preset_obj);
            Py_XDECREF(hint_obj);

            if (result == nullptr) {
                PyErr_Print();
                set_action_message(QStringLiteral("無法執行批次重命名，請查看終端記錄。"));
            } else {
                set_action_busy(true);
                set_action_message(auto_accept ? QStringLiteral("資料夾批次分析與一鍵改名執行中…") : QStringLiteral("資料夾批次分析執行中…"));
                Py_DECREF(result);
            }
        }
        PyGILState_Release(gil);
    }

    void batch_undo_action() {
        bool ok = false;
        const QString batch_id = QInputDialog::getText(
            history_window_,
            QStringLiteral("一鍵復原批次改名"),
            QStringLiteral("輸入要復原的 Batch ID（例如：batch_20260905_120000_a1b2c3）:"),
            QLineEdit::Normal,
            QString(),
            &ok
        );
        if (!ok || batch_id.trimmed().isEmpty()) return;

        PyGILState_STATE gil = PyGILState_Ensure();
        PyObject *batch_obj = PyUnicode_FromString(batch_id.trimmed().toUtf8().constData());
        if (batch_obj != nullptr) {
            PyObject *result = PyObject_CallMethod(controller_, "request_batch_undo", "O", batch_obj);
            Py_DECREF(batch_obj);
            if (result == nullptr) {
                PyErr_Print();
                set_action_message(QStringLiteral("無法執行批次復原，請查看終端記錄。"));
            } else {
                set_action_busy(true);
                set_action_message(QStringLiteral("批次復原執行中…"));
                Py_DECREF(result);
            }
        }
        PyGILState_Release(gil);
    }

    void set_error(const QString &message) {
        model_label_->setText(QStringLiteral("模型狀態：錯誤"));
        error_label_->setText(message);
        error_label_->show();
        show_home();
        if (tray_icon_ != nullptr && QSystemTrayIcon::supportsMessages()) {
            tray_icon_->showMessage(
                QStringLiteral("命圖 AI 處理失敗"),
                message,
                QSystemTrayIcon::Critical,
                10000);
        }
    }

    void save_history_screenshot(const QString &path) {
        show_history();
        history_window_->grab().save(path);
    }

    bool start_controller(PyObject *bridge) {
        PyObject *result = PyObject_CallMethod(controller_, "start", "O", bridge);
        if (result == nullptr) {
            return false;
        }
        Py_DECREF(result);
        return true;
    }

    void request_quit() {
        stop_controller();
        QCoreApplication::quit();
    }

private:
    void build_main_window() {
        main_window_ = new QMainWindow();
        main_window_->setWindowTitle(QStringLiteral("命圖 AI"));
        main_window_->resize(940, 620);

        auto *central = new QWidget(main_window_);
        auto *layout = new QVBoxLayout(central);
        auto *heading = new QLabel(QStringLiteral("命圖 AI｜本地檔案命名助手"), central);
        QFont heading_font = heading->font();
        heading_font.setPointSize(18);
        heading_font.setBold(true);
        heading->setFont(heading_font);
        layout->addWidget(heading);

        auto *description = new QLabel(
            QStringLiteral("檔案出現後才按需載入本地模型；只有你按下接受後才會改名。"),
            central);
        description->setWordWrap(true);
        layout->addWidget(description);

        monitor_label_ = new QLabel(QStringLiteral("監控狀態：監控中"), central);
        pending_label_ = new QLabel(QStringLiteral("待處理檔案：0"), central);
        model_label_ = new QLabel(
            QStringLiteral("模型狀態：待命（有工作時按需載入）"),
            central);
        error_label_ = new QLabel(central);
        error_label_->setWordWrap(true);
        error_label_->setStyleSheet(QStringLiteral("color: #c0392b;"));
        error_label_->hide();
        layout->addWidget(monitor_label_);
        layout->addWidget(pending_label_);
        layout->addWidget(model_label_);
        layout->addWidget(error_label_);
        proposal_scope_ = new QLabel(QStringLiteral("待決建議：0 筆（本次啟動）"), central);
        proposal_scope_->setWordWrap(true);
        layout->addWidget(proposal_scope_);
        proposals_ = make_table(central, {QStringLiteral("原檔名"), QStringLiteral("建議檔名"), QStringLiteral("所在位置")});
        layout->addWidget(proposals_, 1);
        auto *proposal_buttons = new QHBoxLayout();
        accept_button_ = new QPushButton(QStringLiteral("接受"), central);
        edit_button_ = new QPushButton(QStringLiteral("修改並接受…"), central);
        ignore_button_ = new QPushButton(QStringLiteral("忽略"), central);
        proposal_buttons->addWidget(accept_button_);
        proposal_buttons->addWidget(edit_button_);
        proposal_buttons->addWidget(ignore_button_);
        proposal_buttons->addStretch();
        layout->addLayout(proposal_buttons);
        action_message_ = new QLabel(QStringLiteral("選取一筆建議後操作；副檔名會自動保留。"), central);
        action_message_->setWordWrap(true);
        layout->addWidget(action_message_);
        QObject::connect(proposals_, &QTableWidget::itemSelectionChanged, [this]() { update_action_buttons(); });
        QObject::connect(accept_button_, &QPushButton::clicked, [this]() { proposal_action(QStringLiteral("accept")); });
        QObject::connect(edit_button_, &QPushButton::clicked, [this]() { proposal_action(QStringLiteral("edit")); });
        QObject::connect(ignore_button_, &QPushButton::clicked, [this]() { proposal_action(QStringLiteral("ignore")); });

        auto *buttons = new QHBoxLayout();
        auto *batch_button = new QPushButton(QStringLiteral("📁 一鍵批次重命名資料夾…"), central);
        batch_button->setStyleSheet(QStringLiteral("font-weight: bold; background-color: #2980b9; color: white; padding: 4px 10px;"));
        auto *history_button = new QPushButton(QStringLiteral("歷史紀錄"), central);
        auto *settings_button = new QPushButton(QStringLiteral("設定"), central);
        buttons->addWidget(batch_button);
        buttons->addWidget(history_button);
        buttons->addWidget(settings_button);
        buttons->addStretch();
        layout->addLayout(buttons);
        QObject::connect(batch_button, &QPushButton::clicked, [this]() { batch_rename_folder_action(); });
        QObject::connect(history_button, &QPushButton::clicked, [this]() { show_history(); });
        QObject::connect(settings_button, &QPushButton::clicked, [this]() { show_settings(); });

        main_window_->setCentralWidget(central);
    }

    static QTableWidget *make_table(QWidget *parent, const QStringList &headers) {
        auto *table = new QTableWidget(0, headers.size(), parent);
        table->setHorizontalHeaderLabels(headers);
        table->setSelectionBehavior(QAbstractItemView::SelectRows);
        table->setSelectionMode(QAbstractItemView::SingleSelection);
        table->setEditTriggers(QAbstractItemView::NoEditTriggers);
        table->verticalHeader()->hide();
        table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
        table->setWordWrap(false);
        return table;
    }

    static qint64 selected_id(QTableWidget *table) {
        const int row = table->currentRow();
        if (row < 0 || table->selectedItems().isEmpty()) return 0;
        return table->item(row, 0)->data(Qt::UserRole).toLongLong();
    }

    void populate(QTableWidget *table, const QJsonArray &rows, bool is_history) {
        const qint64 selected = selected_id(table);
        const int vertical_scroll = table->verticalScrollBar()->value();
        const int horizontal_scroll = table->horizontalScrollBar()->value();
        QSignalBlocker blocked(table);
        table->clearContents();
        table->setRowCount(rows.size());
        table->clearSelection();
        table->setCurrentCell(-1, -1);
        int selected_row = -1;
        int row = 0;
        for (const auto &value : rows) {
            const QJsonObject data = value.toObject();
            const qint64 id = data.value(QStringLiteral("id")).toInteger();
            const QStringList fields = is_history
                ? QStringList{data.value("time").toString(), data.value("state").toString(), data.value("source").toString(), data.value("target").toString(), data.value("message").toString()}
                : QStringList{data.value("source").toString(), data.value("suggested").toString(), data.value("path").toString()};
            for (int column = 0; column < fields.size(); ++column) {
                auto *item = new QTableWidgetItem(fields[column]);
                item->setToolTip(data.value("path").toString() + "\n" + fields[column]);
                item->setData(Qt::UserRole, id);
                item->setData(Qt::UserRole + 1, data.toVariantMap());
                table->setItem(row, column, item);
            }
            if (id == selected) selected_row = row;
            ++row;
        }
        if (selected_row >= 0) table->selectRow(selected_row);
        table->verticalScrollBar()->setValue(vertical_scroll);
        table->horizontalScrollBar()->setValue(horizontal_scroll);
    }

    void update_action_buttons() {
        if (accept_button_ == nullptr || undo_button_ == nullptr) return;
        const bool proposal_selected = selected_id(proposals_) != 0 && !action_busy_;
        accept_button_->setEnabled(proposal_selected);
        edit_button_->setEnabled(proposal_selected);
        ignore_button_->setEnabled(proposal_selected);
        bool undoable = false;
        if (selected_id(history_) != 0) {
            undoable = history_->item(history_->currentRow(), 0)->data(Qt::UserRole + 1).toMap().value("undoable").toBool();
        }
        undo_button_->setEnabled(undoable && !action_busy_);
    }

    void submit_action(const QString &action, qint64 id, const QString *stem = nullptr) {
        PyGILState_STATE gil = PyGILState_Ensure();
        const QByteArray name = action.toUtf8();
        PyObject *edited = stem == nullptr ? Py_NewRef(Py_None) : PyUnicode_FromString(stem->toUtf8().constData());
        PyObject *result = edited == nullptr ? nullptr : PyObject_CallMethod(controller_, "request_action", "sLO", name.constData(), static_cast<long long>(id), edited);
        Py_XDECREF(edited);
        if (result == nullptr) {
            PyErr_Print();
            set_action_message(QStringLiteral("無法執行操作，請查看終端記錄。"));
        } else {
            const int accepted = PyObject_IsTrue(result);
            if (accepted > 0) {
                set_action_busy(true);
                set_action_message(QStringLiteral("操作中…"));
            } else {
                if (accepted < 0) PyErr_Print();
                set_action_message(QStringLiteral("目前已有操作執行中，請稍後再試。"));
            }
            Py_DECREF(result);
        }
        PyGILState_Release(gil);
    }

    void proposal_action(const QString &action) {
        const qint64 id = selected_id(proposals_);
        if (id == 0 || action_busy_) return;
        if (action == QStringLiteral("edit")) {
            const QString stem = proposals_->item(proposals_->currentRow(), 0)->data(Qt::UserRole + 1).toMap().value("stem").toString();
            bool accepted = false;
            const QString edited = QInputDialog::getText(main_window_, QStringLiteral("修改建議檔名"), QStringLiteral("輸入檔名主體（副檔名會自動保留）"), QLineEdit::Normal, stem, &accepted);
            if (accepted && !edited.trimmed().isEmpty()) submit_action(action, id, &edited);
        } else submit_action(action, id);
    }

    void build_history_window() {
        history_window_ = new QMainWindow();
        history_window_->setWindowTitle(QStringLiteral("命圖 AI｜歷史紀錄"));
        history_window_->resize(1040, 520);
        auto *central = new QWidget(history_window_);
        auto *layout = new QVBoxLayout(central);
        auto *scope = new QLabel(QStringLiteral("最近 100 筆事件；選取仍可復原的改名紀錄後操作。原名稱被占用時不會覆寫。"), central);
        scope->setWordWrap(true);
        layout->addWidget(scope);
        history_ = make_table(central, {QStringLiteral("時間"), QStringLiteral("事件／狀態"), QStringLiteral("原檔名"), QStringLiteral("目的／建議檔名"), QStringLiteral("詳情")});
        layout->addWidget(history_, 1);

        auto *hist_buttons = new QHBoxLayout();
        undo_button_ = new QPushButton(QStringLiteral("復原選取的改名"), central);
        auto *batch_undo_btn = new QPushButton(QStringLiteral("一鍵復原批次改名…"), central);
        hist_buttons->addWidget(undo_button_);
        hist_buttons->addWidget(batch_undo_btn);
        hist_buttons->addStretch();
        layout->addLayout(hist_buttons);

        history_message_ = new QLabel(central);
        history_message_->setWordWrap(true);
        layout->addWidget(history_message_);
        history_window_->setCentralWidget(central);
        QObject::connect(history_, &QTableWidget::itemSelectionChanged, [this]() { update_action_buttons(); });
        QObject::connect(undo_button_, &QPushButton::clicked, [this]() { submit_action(QStringLiteral("undo"), selected_id(history_)); });
        QObject::connect(batch_undo_btn, &QPushButton::clicked, [this]() { batch_undo_action(); });
        update_action_buttons();
    }

    void build_settings_window() {
        settings_window_ = new QMainWindow();
        settings_window_->setWindowTitle(QStringLiteral("命圖 AI｜設定"));
        settings_window_->resize(680, 500);
        auto *central = new QWidget(settings_window_);
        auto *layout = new QVBoxLayout(central);

        auto *watch_label = new QLabel(QStringLiteral("監控資料夾管理（新增或移除常駐檔案監控目錄）："), central);
        watch_label->setStyleSheet(QStringLiteral("font-weight: bold;"));
        layout->addWidget(watch_label);

        watch_list_ = new QListWidget(central);
        layout->addWidget(watch_list_, 1);

        auto *btn_layout = new QHBoxLayout();
        add_watch_button_ = new QPushButton(QStringLiteral("新增監控資料夾..."), central);
        remove_watch_button_ = new QPushButton(QStringLiteral("移除選取的資料夾"), central);
        btn_layout->addWidget(add_watch_button_);
        btn_layout->addWidget(remove_watch_button_);
        layout->addLayout(btn_layout);

        auto *info_label = new QLabel(QStringLiteral("系統與模型狀態摘要："), central);
        info_label->setStyleSheet(QStringLiteral("font-weight: bold; margin-top: 8px;"));
        layout->addWidget(info_label);

        settings_text_ = new QTextEdit(central);
        settings_text_->setReadOnly(true);
        settings_text_->setMaximumHeight(130);
        layout->addWidget(settings_text_);

        settings_window_->setCentralWidget(central);

        QObject::connect(add_watch_button_, &QPushButton::clicked, [this]() { add_watch_directory_action(); });
        QObject::connect(remove_watch_button_, &QPushButton::clicked, [this]() { remove_watch_directory_action(); });
    }

    void build_tray() {
        auto *menu = new QMenu(main_window_);
        auto *open_action = menu->addAction(QStringLiteral("開啟命圖 AI"));
        pause_action_ = menu->addAction(QStringLiteral("暫停監控"));
        pending_action_ = menu->addAction(QStringLiteral("待處理檔案：0"));
        pending_action_->setEnabled(false);
        auto *batch_action = menu->addAction(QStringLiteral("📁 一鍵批次重命名資料夾…"));
        auto *history_action = menu->addAction(QStringLiteral("歷史紀錄"));
        auto *settings_action = menu->addAction(QStringLiteral("設定"));
        menu->addSeparator();
        auto *quit_action = menu->addAction(QStringLiteral("離開"));

        tray_icon_ = new QSystemTrayIcon(main_window_);
        QIcon icon = QIcon::fromTheme(QStringLiteral("folder-documents-symbolic"));
        if (icon.isNull()) {
            icon = QApplication::style()->standardIcon(QStyle::SP_DirIcon);
        }
        tray_icon_->setIcon(icon);
        tray_icon_->setToolTip(QStringLiteral("命圖 AI｜本地檔案命名助手"));
        tray_icon_->setContextMenu(menu);

        QObject::connect(open_action, &QAction::triggered, [this]() { show_home(); });
        QObject::connect(batch_action, &QAction::triggered, [this]() { batch_rename_folder_action(); });
        QObject::connect(history_action, &QAction::triggered, [this]() { show_history(); });
        QObject::connect(settings_action, &QAction::triggered, [this]() { show_settings(); });
        QObject::connect(quit_action, &QAction::triggered, [this]() { request_quit(); });
        QObject::connect(pause_action_, &QAction::triggered, [this]() { toggle_pause(); });
        QObject::connect(
            tray_icon_,
            &QSystemTrayIcon::activated,
            [this](QSystemTrayIcon::ActivationReason reason) {
                if (reason == QSystemTrayIcon::Trigger || reason == QSystemTrayIcon::DoubleClick) {
                    show_home();
                }
            });
        if (QSystemTrayIcon::isSystemTrayAvailable()) {
            tray_icon_->show();
        }
    }

    void toggle_pause() {
        PyGILState_STATE gil = PyGILState_Ensure();
        PyObject *result = PyObject_CallMethod(controller_, "toggle_pause", nullptr);
        if (result == nullptr) {
            PyErr_Print();
            set_error(QStringLiteral("無法切換監控狀態，請查看終端記錄。"));
        } else {
            const int truth = PyObject_IsTrue(result);
            Py_DECREF(result);
            if (truth >= 0) {
                set_paused(truth != 0);
            } else {
                PyErr_Print();
            }
        }
        PyGILState_Release(gil);
    }

    void stop_controller() {
        if (stop_called_) {
            return;
        }
        stop_called_ = true;
        PyGILState_STATE gil = PyGILState_Ensure();
        PyObject *result = PyObject_CallMethod(controller_, "stop", nullptr);
        if (result == nullptr) {
            PyErr_Print();
        } else {
            Py_DECREF(result);
        }
        PyGILState_Release(gil);
    }

    PyObject *controller_ = nullptr;
    QMainWindow *main_window_ = nullptr;
    QMainWindow *history_window_ = nullptr;
    QMainWindow *settings_window_ = nullptr;
    QLabel *monitor_label_ = nullptr;
    QLabel *pending_label_ = nullptr;
    QLabel *model_label_ = nullptr;
    QLabel *error_label_ = nullptr;
    QTableWidget *proposals_ = nullptr;
    QTableWidget *history_ = nullptr;
    QPushButton *accept_button_ = nullptr;
    QPushButton *edit_button_ = nullptr;
    QPushButton *ignore_button_ = nullptr;
    QPushButton *undo_button_ = nullptr;
    QLabel *action_message_ = nullptr;
    QLabel *proposal_scope_ = nullptr;
    QLabel *history_message_ = nullptr;
    QString proposals_json_;
    QString history_json_;
    bool action_busy_ = false;
    QTextEdit *settings_text_ = nullptr;
    QListWidget *watch_list_ = nullptr;
    QPushButton *add_watch_button_ = nullptr;
    QPushButton *remove_watch_button_ = nullptr;
    QString watch_directories_json_;
    QSystemTrayIcon *tray_icon_ = nullptr;
    QAction *pause_action_ = nullptr;
    QAction *pending_action_ = nullptr;
    bool paused_ = false;
    bool stop_called_ = false;
};

PyObject *bridge_set_pending_count(BridgeObject *self, PyObject *args) {
    int count = 0;
    if (!PyArg_ParseTuple(args, "i", &count) || !bridge_is_live(self)) {
        return nullptr;
    }
    Dashboard *dashboard = self->dashboard;
    QMetaObject::invokeMethod(
        dashboard,
        [dashboard, count]() { dashboard->set_pending_count(count); },
        Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_set_model_status(BridgeObject *self, PyObject *args) {
    PyObject *value = nullptr;
    if (!PyArg_ParseTuple(args, "U", &value) || !bridge_is_live(self)) {
        return nullptr;
    }
    const QString text = py_unicode_to_qstring(value);
    if (PyErr_Occurred()) {
        return nullptr;
    }
    Dashboard *dashboard = self->dashboard;
    QMetaObject::invokeMethod(
        dashboard,
        [dashboard, text]() { dashboard->set_model_status(text); },
        Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_set_paused(BridgeObject *self, PyObject *args) {
    int paused = 0;
    if (!PyArg_ParseTuple(args, "p", &paused) || !bridge_is_live(self)) {
        return nullptr;
    }
    Dashboard *dashboard = self->dashboard;
    QMetaObject::invokeMethod(
        dashboard,
        [dashboard, paused]() { dashboard->set_paused(paused != 0); },
        Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_set_text(
    BridgeObject *self,
    PyObject *args,
    void (Dashboard::*setter)(const QString &)) {
    PyObject *value = nullptr;
    if (!PyArg_ParseTuple(args, "U", &value) || !bridge_is_live(self)) {
        return nullptr;
    }
    const QString text = py_unicode_to_qstring(value);
    if (PyErr_Occurred()) {
        return nullptr;
    }
    Dashboard *dashboard = self->dashboard;
    QMetaObject::invokeMethod(
        dashboard,
        [dashboard, text, setter]() { (dashboard->*setter)(text); },
        Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_set_proposals_json(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::set_proposals_json);
}

PyObject *bridge_set_history_json(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::set_history_json);
}

PyObject *bridge_set_action_message(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::set_action_message);
}

PyObject *bridge_save_screenshot(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::save_screenshot);
}

PyObject *bridge_save_history_screenshot(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::save_history_screenshot);
}

PyObject *bridge_finish_smoke(BridgeObject *self, PyObject *) {
    if (!bridge_is_live(self)) return nullptr;
    Dashboard *dashboard = self->dashboard;
    QMetaObject::invokeMethod(dashboard, [dashboard]() { dashboard->request_quit(); }, Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_set_action_busy(BridgeObject *self, PyObject *args) {
    int busy = 0;
    if (!PyArg_ParseTuple(args, "p", &busy) || !bridge_is_live(self)) return nullptr;
    Dashboard *dashboard = self->dashboard;
    QMetaObject::invokeMethod(dashboard, [dashboard, busy]() { dashboard->set_action_busy(busy != 0); }, Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_test_action(BridgeObject *self, PyObject *args) {
    const char *action = nullptr;
    long long id = 0;
    const char *stem = "";
    if (!PyArg_ParseTuple(args, "sL|s", &action, &id, &stem) || !bridge_is_live(self)) return nullptr;
    Dashboard *dashboard = self->dashboard;
    const QString action_text = QString::fromUtf8(action);
    const QString stem_text = QString::fromUtf8(stem);
    QMetaObject::invokeMethod(dashboard, [dashboard, action_text, id, stem_text]() { dashboard->test_action(action_text, id, stem_text); }, Qt::QueuedConnection);
    Py_RETURN_NONE;
}

PyObject *bridge_set_settings_text(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::set_settings_text);
}

PyObject *bridge_set_watch_directories_json(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::set_watch_directories_json);
}

PyObject *bridge_set_error(BridgeObject *self, PyObject *args) {
    return bridge_set_text(self, args, &Dashboard::set_error);
}

PyMethodDef BridgeMethods[] = {
    {"set_pending_count", reinterpret_cast<PyCFunction>(bridge_set_pending_count), METH_VARARGS,
     "Set the pending-file count using a queued GUI-thread invocation."},
    {"set_model_status", reinterpret_cast<PyCFunction>(bridge_set_model_status), METH_VARARGS,
     "Set the human-readable local model state."},
    {"set_paused", reinterpret_cast<PyCFunction>(bridge_set_paused), METH_VARARGS,
     "Update paused/resumed labels and actions."},
    {"set_proposals_json", reinterpret_cast<PyCFunction>(bridge_set_proposals_json), METH_VARARGS, "Update pending proposals by stable event ID."},
    {"set_history_json", reinterpret_cast<PyCFunction>(bridge_set_history_json), METH_VARARGS, "Update recent history and undo eligibility."},
    {"set_watch_directories_json", reinterpret_cast<PyCFunction>(bridge_set_watch_directories_json), METH_VARARGS, "Update monitored directories list in settings."},
    {"set_action_busy", reinterpret_cast<PyCFunction>(bridge_set_action_busy), METH_VARARGS, "Enable actions when the worker is idle."},
    {"set_action_message", reinterpret_cast<PyCFunction>(bridge_set_action_message), METH_VARARGS, "Display action results and refresh errors."},
    {"_test_action", reinterpret_cast<PyCFunction>(bridge_test_action), METH_VARARGS, "Private synthetic smoke button callback hook."},
    {"_save_screenshot", reinterpret_cast<PyCFunction>(bridge_save_screenshot), METH_VARARGS, "Capture only the app home widget for layout QA."},
    {"request_quit", reinterpret_cast<PyCFunction>(bridge_finish_smoke), METH_NOARGS, "Queue shutdown through the normal quit handler."},
    {"_finish_smoke", reinterpret_cast<PyCFunction>(bridge_finish_smoke), METH_NOARGS, "Private smoke shutdown through the normal quit handler."},
    {"_save_history_screenshot", reinterpret_cast<PyCFunction>(bridge_save_history_screenshot), METH_VARARGS, "Capture only the app history widget for layout QA."},
    {"set_settings_text", reinterpret_cast<PyCFunction>(bridge_set_settings_text), METH_VARARGS,
     "Replace the read-only settings view."},
    {"set_error", reinterpret_cast<PyCFunction>(bridge_set_error), METH_VARARGS,
     "Show a visible error in the home view."},
    {nullptr, nullptr, 0, nullptr},
};

PyObject *native_run(PyObject *, PyObject *args, PyObject *kwargs) {
    PyObject *controller = nullptr;
    const char *app_name = "Mingtu AI";
    int quit_after_ms = 0;
    int show_windows = 1;
    static const char *keywords[] = {
        "controller", "app_name", "quit_after_ms", "show_windows", nullptr};
    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "O|sip:run",
            const_cast<char **>(keywords),
            &controller,
            &app_name,
            &quit_after_ms,
            &show_windows)) {
        return nullptr;
    }
    if (quit_after_ms < 0) {
        PyErr_SetString(PyExc_ValueError, "quit_after_ms must be non-negative");
        return nullptr;
    }
    if (QCoreApplication::instance() != nullptr) {
        PyErr_SetString(PyExc_RuntimeError, "a Qt application instance already exists");
        return nullptr;
    }

    char executable[] = "mingtu-ai";
    char *qt_argv[] = {executable, nullptr};
    int qt_argc = 1;
    try {
        QApplication application(qt_argc, qt_argv);
        Dashboard dashboard(controller, QString::fromUtf8(app_name), show_windows != 0);

        auto *bridge = PyObject_New(BridgeObject, &BridgeType);
        if (bridge == nullptr) {
            return nullptr;
        }
        bridge->dashboard = &dashboard;
        if (!dashboard.start_controller(reinterpret_cast<PyObject *>(bridge))) {
            // start() may have created one of its owned worker threads before
            // raising.  Give the controller the same idempotent cleanup path
            // used by a normal Qt shutdown before unwinding the extension.
            PyObject *error_type = nullptr;
            PyObject *error_value = nullptr;
            PyObject *error_traceback = nullptr;
            PyErr_Fetch(&error_type, &error_value, &error_traceback);
            dashboard.request_quit();
            if (PyErr_Occurred()) {
                PyErr_Clear();
            }
            PyErr_Restore(error_type, error_value, error_traceback);
            bridge->dashboard = nullptr;
            Py_DECREF(bridge);
            return nullptr;
        }
        if (quit_after_ms > 0) {
            QTimer::singleShot(quit_after_ms, [&dashboard]() { dashboard.request_quit(); });
        }

        PyObject *signal_error = nullptr;
        QTimer sigint_timer;
        QObject::connect(&sigint_timer, &QTimer::timeout, [&dashboard, &signal_error]() {
            PyGILState_STATE gstate = PyGILState_Ensure();
            if (PyErr_CheckSignals() != 0) {
                // The controller cleanup calls Python and needs a clear error state.
                signal_error = PyErr_GetRaisedException();
                dashboard.request_quit();
            }
            PyGILState_Release(gstate);
        });
        sigint_timer.start(200);

        int exit_code = 0;
        Py_BEGIN_ALLOW_THREADS
        exit_code = application.exec();
        Py_END_ALLOW_THREADS

        sigint_timer.stop();

        bridge->dashboard = nullptr;
        Py_DECREF(bridge);
        if (signal_error != nullptr) {
            PyErr_SetRaisedException(signal_error);
            return nullptr;
        }
        return PyLong_FromLong(exit_code);
    } catch (const std::exception &error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "Qt UI failed with an unknown native exception");
        return nullptr;
    }
}

PyMethodDef ModuleMethods[] = {
    {"run", reinterpret_cast<PyCFunction>(native_run), METH_VARARGS | METH_KEYWORDS,
     "Run the Qt6 application event loop in this Python process."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef ModuleDefinition = {
    PyModuleDef_HEAD_INIT,
    "_native_ui",
    "Same-process Qt6 product shell for Mingtu AI.",
    -1,
    ModuleMethods,
};

}  // namespace

PyMODINIT_FUNC PyInit__native_ui() {
    BridgeType.tp_name = "mingtu_ai.ui._native_ui.Bridge";
    BridgeType.tp_basicsize = sizeof(BridgeObject);
    BridgeType.tp_flags = Py_TPFLAGS_DEFAULT;
    BridgeType.tp_methods = BridgeMethods;
    // Bridge instances carry a non-owning Dashboard pointer and must only be
    // created by run(); exposing a public constructor could create an invalid
    // object that has no live Qt target.
    BridgeType.tp_new = nullptr;
    if (PyType_Ready(&BridgeType) < 0) {
        return nullptr;
    }

    PyObject *module = PyModule_Create(&ModuleDefinition);
    if (module == nullptr) {
        return nullptr;
    }
    Py_INCREF(&BridgeType);
    if (PyModule_AddObject(module, "Bridge", reinterpret_cast<PyObject *>(&BridgeType)) < 0) {
        Py_DECREF(&BridgeType);
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}
